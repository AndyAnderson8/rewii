"""Tests for rewii.core.nus with a mocked NUS (no network)."""
from __future__ import annotations


import httpx
import pytest
from libWiiPy.title import Title

from rewii.core import net, nus
from rewii.core.config import Settings
from rewii.core.layout import DriveLayout
from rewii.core.wad import Refused, inspect_wad

from test_wad import CERTS, make_title  # noqa: E402  (tests dir is on sys.path)

CA, CP, XS = CERTS[:0x400], CERTS[0x400:0x700], CERTS[0x700:0xA00]


class FakeNus:
    """Serves synthetic titles the way NUS does: tmd(+CP+CA), cetk(ticket+XS+CA), contents."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []

    def add(self, title: Title, *, ticket: bool = True) -> None:
        tid = title.tmd.title_id.lower()
        tmd = title.tmd.dump()
        self.files[f"{tid}/tmd.{title.tmd.title_version}"] = tmd + CP + CA
        self.files[f"{tid}/tmd"] = tmd + CP + CA
        if ticket:
            self.files[f"{tid}/cetk"] = title.ticket.dump() + XS + CA
        for rec, data in zip(title.tmd.content_records, title.content.content_list):
            self.files[f"{tid}/{rec.content_id:08x}"] = data

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.url.path.split("/ccs/download/", 1)[1]
        if key in self.files:
            return httpx.Response(200, content=self.files[key])
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch):
    f = FakeNus()
    net.set_client(httpx.Client(transport=httpx.MockTransport(f.handler)))
    # synthetic certificates can't carry Nintendo's real signatures
    monkeypatch.setattr(Title, "get_is_signed", lambda self: True)
    yield f
    net.set_client(None)


def ios(n: int, ver: int, vwii: bool = False, ckey: int = 0) -> Title:
    high = "00000007" if vwii else "00000001"
    return make_title(f"{high}{n:08x}", version=ver, ios=0, region=3, name=None, ckey=ckey,
                      contents=[bytes([n]) * 300, b"x" * 1000], fakesign=False)


WII = Settings(console_type="wii", console_region="NTSC-U")
VWII = Settings(console_type="vwii", console_region="PAL")


def test_download_title_wad(fake, tmp_path):
    fake.add(ios(38, 4123))
    events = []
    p = nus.download_title_wad("0000000100000026", 4123, tmp_path / "a.wad", progress=events.append)
    info = inspect_wad(p)
    assert info.title_id == "0000000100000026" and info.version == 4123 and info.hashes_ok
    assert all(r.headers["user-agent"] == nus.NUS_USER_AGENT for r in fake.requests)
    assert events[-1].done == events[-1].total > 0
    assert not list(tmp_path.glob("*.part"))


def test_bad_content_hash(fake, tmp_path):
    t = ios(38, 4123)
    fake.add(t)
    key = next(k for k in fake.files if k.endswith("/00000001"))
    fake.files[key] = bytes(len(fake.files[key]))
    with pytest.raises(nus.NusError):
        nus.download_title_wad("0000000100000026", 4123, tmp_path / "a.wad")
    assert not (tmp_path / "a.wad").exists()


def test_unsigned_rejected(fake, monkeypatch, tmp_path):
    fake.add(ios(38, 4123))
    monkeypatch.setattr(Title, "get_is_signed", lambda self: False)
    with pytest.raises(nus.NusError, match="signature"):
        nus.download_title("0000000100000026", 4123)


def test_missing_ticket_and_version(fake, tmp_path):
    fake.add(ios(38, 4123), ticket=False)
    with pytest.raises(nus.NusError, match="ticket"):
        nus.download_title("0000000100000026", 4123)
    with pytest.raises(nus.NusError, match="does not exist"):
        nus.download_title("0000000100000026", 9999)


def test_cert_chain_layout():
    t = ios(38, 4123)
    raw_tmd = t.tmd.dump() + CP + CA
    cetk = t.ticket.dump() + XS + CA
    assert nus.build_cert_chain(raw_tmd, cetk) == CA + CP + XS


def test_rekey_for_wii(fake):
    fake.add(ios(38, 4380, vwii=True, ckey=2))
    t = nus.download_title("0000000700000026", 4380)
    key = t.ticket.get_title_key()
    nus.rekey_for_wii(t)
    assert t.ticket.common_key_index == 0
    assert t.ticket.get_title_key() == key
    assert t.get_content_by_index(0) == bytes([38]) * 300


def test_catalog_values():
    assert [e.filename for e in nus.CIOS_BASE["wii"]] == [
        "IOS38-64-v4123.wad", "IOS56-64-v5661.wad", "IOS57-64-v5918.wad", "IOS58-64-v6175.wad"]
    assert [e.filename for e in nus.CIOS_BASE["vwii"]] == [
        "IOS38-64-v4380.wad", "IOS56-64-v5918.wad", "IOS57-64-v6175.wad", "IOS58-64-v6432.wad"]
    assert all(e.title_id.startswith("00000007") and e.wii_common_key for e in nus.CIOS_BASE["vwii"])
    assert nus.CIOS_BASE["wii"][3].title_id == "000000010000003a"
    assert nus.SYSTEM_MENU_43 == {"NTSC-U": 513, "PAL": 514, "NTSC-J": 512, "NTSC-K": 518}


@pytest.mark.parametrize("settings", [
    Settings(), Settings(console_type="wii"), Settings(console_type="mini", console_region="NTSC-U")])
def test_cios_refused(settings, tmp_path):
    with pytest.raises(Refused):
        nus.prepare_cios_files(DriveLayout(tmp_path), settings)


@pytest.mark.parametrize("settings", [
    Settings(), Settings(console_type="vwii", console_region="NTSC-U"),
    Settings(console_type="mini", console_region="PAL"), Settings(console_type="wii")])
def test_update_refused(settings, tmp_path):
    with pytest.raises(Refused):
        nus.prepare_update_43(DriveLayout(tmp_path), settings)


def test_prepare_cios_wii(fake, tmp_path):
    for e in nus.CIOS_BASE["wii"]:
        fake.add(ios(int(e.title_id[8:], 16), e.version))
    layout = DriveLayout(tmp_path / "usb")
    files = nus.prepare_cios_files(layout, WII)
    assert [f.name for f in files] == [e.filename for e in nus.CIOS_BASE["wii"]]
    assert all(f.parent == layout.root for f in files)
    n = len(fake.requests)
    nus.prepare_cios_files(layout, WII)          # already present -> no downloads
    assert len(fake.requests) == n


def test_prepare_cios_vwii_rekeys(fake, tmp_path):
    for e in nus.CIOS_BASE["vwii"]:
        fake.add(ios(int(e.title_id[8:], 16), e.version, vwii=True, ckey=2))
    files = nus.prepare_cios_files(DriveLayout(tmp_path), VWII)
    for f in files:
        info = inspect_wad(f)
        assert info.common_key_index == 0 and info.hashes_ok and info.title_id.startswith("00000007")


def test_prepare_update_43(fake, tmp_path):
    for e in nus.UPDATE_43_IOS:
        fake.add(ios(int(e.title_id[8:], 16), e.version))
    fake.add(make_title("0000000100000002", version=514, ios=80, region=2, name=None,
                        contents=[b"menu" * 100], fakesign=False))
    layout = DriveLayout(tmp_path)
    pal = Settings(console_type="family", console_region="PAL")
    files = nus.prepare_update_43(layout, pal)
    assert all(f.parent == layout.wad for f in files)
    names = [f.name for f in files]
    assert names == ["IOS80-64-v6944.wad", "IOS58-64-v6176.wad", "SystemMenu-4.3E-v514.wad"]


def test_wrong_region_menu_never_written(fake, tmp_path, monkeypatch):
    # NUS sending a US menu for a PAL request must be caught by the risk check.
    fake.add(make_title("0000000100000002", version=513, ios=80, region=1, name=None,
                        contents=[b"menu" * 100], fakesign=False))
    with pytest.raises(Refused):
        nus.download_title_wad("0000000100000002", 513, tmp_path / "sm.wad",
                               settings=Settings(console_type="wii", console_region="PAL"))
    assert not (tmp_path / "sm.wad").exists()


def test_theme_base(fake, tmp_path):
    menu = make_title("0000000700000002", version=609, ios=80, region=0, name=None,
                      contents=[b"a" * 50] * 0x1F + [b"THEME" * 1000], fakesign=False)
    # give content index 0x1F the id 0x1F (make_title uses cid == index)
    fake.add(menu)
    p = nus.download_theme_base(Settings(console_type="vwii", console_region="NTSC-U"), tmp_path / "themes")
    assert p.name == "0000001F.app" and p.read_bytes() == b"THEME" * 1000
    with pytest.raises(Refused):
        nus.theme_base(Settings(console_type="vwii", console_region="NTSC-K"))
    with pytest.raises(Refused):
        nus.theme_base(Settings(console_type="mini", console_region="PAL"))
    assert nus.theme_base(Settings(console_type="wii", console_region="PAL")).filename == "0000009A.app"
