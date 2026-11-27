"""Tests for rewii.core.cheats (network mocked)."""
from __future__ import annotations

import httpx
import pytest

from rewii.core import cheats, config, net
from rewii.core.cheats import CheatError, build_gct, parse_txtcodes
from rewii.core.layout import DriveLayout

TXT = (
    "RMCE01\r\nMario Kart Wii\r\n\r\n"
    "Everlasting Items v2 [Anarion]\r\n"
    "C26FE200 00000001\r\n"
    "60000000 00000000\r\n"
    "https://example.invalid/video\r\n"
    "Does not work on blue shell.\r\n\r\n"
    "Activator Code [Anarion]\r\n"
    "2834XXXX YYYYZZZZ\r\n"
    "0442785C ZZZZzzzz\r\n"
    "Fill in button activator placeholders.\r\n"
    "00 -your first mii\r\n\r\n"
    "Just A Header\r\n\r\n"
    "Simple [me]\r\n"
    "0279FC86 0000FFFF\r\n"
)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setattr(config, "cache_dir", lambda: d)
    yield d
    net.set_client(None)


def serve(pages: dict[str, bytes], log=None):
    def handler(req):
        if log is not None:
            log.append(str(req.url))
        u = str(req.url)
        return httpx.Response(200, content=pages[u]) if u in pages else httpx.Response(404)
    net.set_client(httpx.Client(transport=httpx.MockTransport(handler)))


def url(gid):
    return cheats.CHEAT_SOURCES[0].format(game_id=gid)


def test_parse():
    cf = parse_txtcodes(TXT)
    assert cf.game_id == "RMCE01" and cf.title == "Mario Kart Wii"
    assert [c.title for c in cf.cheats] == ["Everlasting Items v2", "Activator Code", "Simple"]
    c0 = cf.cheats[0]
    assert c0.author == "Anarion"
    assert c0.codes == [("C26FE200", "00000001"), ("60000000", "00000000")]
    assert "blue shell" in c0.comment and not c0.needs_values
    c1 = cf.cheats[1]
    assert c1.placeholders == {"X": 4, "Y": 4, "Z": 4, "z": 4}
    assert "00 -your first mii" in c1.comment
    assert len(c1.codes) == 2


def test_fill_and_gct():
    cf = parse_txtcodes(TXT)
    c1 = cf.cheats[1]
    with pytest.raises(CheatError):
        c1.filled({"X": "1"})
    with pytest.raises(CheatError):
        c1.filled({"X": "12345", "Y": 0, "Z": 0, "z": 0})
    assert c1.filled({"X": "0001", "Y": 0x8000, "Z": "ab", "z": "0x1"}) == [
        ("28340001", "800000AB"), ("0442785C", "00AB0001")]
    data = cheats.gct_bytes([cf.cheats[0], cf.cheats[2]])
    assert data == bytes.fromhex(
        "00D0C0DE00D0C0DE" "C26FE20000000001" "6000000000000000"
        "0279FC860000FFFF" "F000000000000000")
    assert cheats.selected_in_gct(cf, data) == [0, 2]


def test_fetch_and_build(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
    (lay.root / "wiiflow").mkdir(parents=True)
    log: list[str] = []
    serve({url("RMCE01"): TXT.encode()}, log)
    cf = cheats.fetch_cheats(lay, "rmce01")
    assert cf is not None and len(cf.cheats) == 3
    assert (lay.txtcodes / "RMCE01.txt").read_bytes() == TXT.encode()
    assert (lay.wiiflow / "codes" / "RMCE01.txt").exists()
    # second call: from the drive, no network
    n = len(log)
    assert cheats.fetch_cheats(lay, "RMCE01") is not None and len(log) == n

    p = build_gct(lay, "RMCE01", [cf.cheats[1]],
                  {cf.cheats[1].name: {"X": 1, "Y": 2, "Z": 3, "z": 4}})
    assert p == lay.codes / "RMCE01.gct"
    assert p.read_bytes()[:8] == cheats.GCT_HEADER
    assert (lay.wiiflow / "codes" / "gct" / "RMCE01.gct").exists()
    with pytest.raises(CheatError):
        build_gct(lay, "RMCE01", [cf.cheats[1]])

    build_gct(lay, "RMCE01", [])
    assert not (lay.codes / "RMCE01.gct").exists()


def test_missing_and_offline(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
    lay.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    serve({url("ZZZE01"): b"<html>nope</html>"})
    assert cheats.fetch_cheats(lay, "ZZZE01", wiiflow=False) is None
    assert cheats.fetch_cheats(lay, "QQQE01", wiiflow=False) is None

    def boom(req):
        raise httpx.ConnectError("offline")
    net.set_client(httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(net.NetError):
        cheats.fetch_cheats(lay, "RMCE01", wiiflow=False)


def test_refresh_offline_uses_cache(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
    lay.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    serve({url("RMCE01"): TXT.encode()})
    cheats.fetch_cheats(lay, "RMCE01", wiiflow=False)

    def boom(req):
        raise httpx.ConnectError("offline")
    net.set_client(httpx.Client(transport=httpx.MockTransport(boom)))
    lay2 = DriveLayout(tmp_path / "usb2")
    lay2.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    cf = cheats.fetch_cheats(lay2, "RMCE01", wiiflow=False, refresh=True)
    assert cf is not None and (lay2.txtcodes / "RMCE01.txt").exists()
    assert cheats.load_cheats(lay2, "RMCE01").title == "Mario Kart Wii"


# --------------------------------------------------------------------------- regressions
def test_selected_in_gct_only_matches_whole_lines():
    cf = parse_txtcodes("GAME01\nT\n\nA\n11111111 22222222\n\nB\n22222222 33333333\n\n"
                        "C\n33333333 44444444\n")
    a, b, c = cf.cheats
    gct = cheats.gct_bytes([a, c])       # ...11111111 22222222 33333333 44444444...
    # B's bytes appear at a 4-byte offset (second half of A + first half of C): not selected
    assert b.to_bytes() in gct and cheats.selected_in_gct(cf, gct) == [0, 2]
    assert cheats.selected_in_gct(cf, cheats.gct_bytes([b])) == [1]
    assert cheats.selected_in_gct(cf, b"junk") == []


def test_fetch_never_raises_cheaterror_and_repairs_bad_drive_copy(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
    lay.txtcodes.mkdir(parents=True)
    (lay.txtcodes / "RMCE01.txt").write_bytes(b"\r\n\r\n")           # unparseable
    assert cheats.load_cheats(lay, "RMCE01") is None
    serve({url("RMCE01"): TXT.encode()})
    cf = cheats.fetch_cheats(lay, "RMCE01", wiiflow=False)
    assert cf is not None and cf.game_id == "RMCE01"
    assert (lay.txtcodes / "RMCE01.txt").read_bytes() == TXT.encode()   # replaced
    (lay.txtcodes / "RSBE01.txt").write_bytes(b"\r\n")
    assert cheats.fetch_cheats(lay, "RSBE01", wiiflow=False) is None    # nothing better online


def test_invalid_ids_never_reach_urls_or_paths(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
    lay.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    log: list[str] = []
    serve({}, log)
    for bad in ("../../x", "RMCE01&a=b", "/etc/x", "??????", "RM E01"):
        assert cheats.fetch_cheats(lay, bad) is None
        assert cheats.download_txtcodes(bad) is None
        assert cheats.load_cheats(lay, bad) is None
        for p in (cheats.txt_path(lay, bad), cheats.gct_path(lay, bad),
                  cheats.wiiflow_txt_path(lay, bad), cheats.wiiflow_gct_path(lay, bad)):
            assert p.parent.name in ("txtcodes", "codes", "gct") and ".." not in p.parts
    assert log == []
    assert cheats.txt_path(lay, "rmce01").name == "RMCE01.txt"
