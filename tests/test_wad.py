"""Tests for rewii.core.wad using tiny synthetic WADs built with libWiiPy."""
from __future__ import annotations

import hashlib
import struct
import sys
from pathlib import Path

import pytest
from libWiiPy.title import Title
from libWiiPy.title.crypto import encrypt_content, encrypt_title_key

from rewii.core import wad as W
from rewii.core.config import Settings
from rewii.core.layout import DriveLayout
from rewii.core.progress import CancelToken


# --------------------------------------------------------------------------- builders
def _cert(sig_type: int, sig_len: int, issuer: str, key_type: int, child: str, key_len: int) -> bytes:
    def pad(b: bytes, n: int = 0x40) -> bytes:
        return b + bytes((-len(b)) % n)
    c = struct.pack(">I", sig_type) + bytes(sig_len)
    c = pad(c)
    c += issuer.encode().ljust(0x40, b"\0") + struct.pack(">I", key_type)
    c += child.encode().ljust(0x40, b"\0") + struct.pack(">I", 1) + b"\x01" * key_len
    if key_type in (0, 1):
        c += struct.pack(">I", 0x10001)
    return pad(c)


CERTS = (_cert(0x10000, 0x200, "Root", 1, "CA00000001", 0x100)
         + _cert(0x10001, 0x100, "Root-CA00000001", 1, "CP00000004", 0x100)
         + _cert(0x10001, 0x100, "Root-CA00000001", 1, "XS00000003", 0x100))


def imet_banner(name: str) -> bytes:
    """Content 0 of a channel: 0x40 bytes, then an IMET header with channel names."""
    from libWiiPy.archive.u8 import IMETHeader
    h = IMETHeader()
    h.create([0, 0, 0], [(1, name), (0, "JP " + name)])
    return h.dump() + b"U\xaa8-" + bytes(0x100)


def make_tmd(tid: str, version: int, ios: int, region: int, records: list[tuple[int, int, int, bytes]],
             vwii: int = 0) -> bytes:
    hdr = bytearray(0x1E4)
    hdr[0:4] = b"\x00\x01\x00\x01"
    issuer = b"Root-CA00000001-CP00000004"
    hdr[0x140:0x140 + len(issuer)] = issuer
    hdr[0x183] = vwii
    hdr[0x184:0x18C] = bytes.fromhex(f"00000001{ios:08x}") if ios else bytes(8)
    hdr[0x18C:0x194] = bytes.fromhex(tid)
    hdr[0x194:0x198] = b"\x00\x00\x00\x01"
    hdr[0x19C:0x19E] = struct.pack(">H", region)
    hdr[0x1DC:0x1E0] = struct.pack(">HH", version, len(records))
    body = b"".join(struct.pack(">IHHQ", cid, idx, ctype, len(data)) + hashlib.sha1(data).digest()
                    for cid, idx, ctype, data in [(r[0], r[1], r[2], r[3]) for r in records])
    return bytes(hdr) + body


def make_ticket(tid: str, title_key: bytes, ckey: int = 0) -> bytes:
    t = bytearray(0x2A4)
    t[0:4] = b"\x00\x01\x00\x01"
    issuer = b"Root-CA00000001-XS00000003"
    t[0x140:0x140 + len(issuer)] = issuer
    t[0x1BF:0x1CF] = encrypt_title_key(title_key, ckey, tid)
    t[0x1DC:0x1E4] = bytes.fromhex(tid)
    t[0x1E4:0x1E6] = b"\xff\xff"
    t[0x1F1] = ckey
    t[0x222:0x262] = b"\xff" * 0x40
    return bytes(t)


def make_title(tid: str = "0001000157414141", *, version: int = 1, ios: int = 58, region: int = 1,
               ckey: int = 0, name: str | None = "Test Channel", contents: list[bytes] | None = None,
               vwii: int = 0, fakesign: bool = True) -> Title:
    key = bytes(range(16))
    if contents is None:
        contents = [imet_banner(name) if name else b"\0" * 64, b"hello world" * 100]
    records = [(i, i, 1, c) for i, c in enumerate(contents)]
    t = Title()
    from libWiiPy.title.cert import CertificateChain
    t.cert_chain = CertificateChain()
    t.cert_chain.load(CERTS)
    t.load_tmd(make_tmd(tid, version, ios, region, records, vwii))
    t.load_ticket(make_ticket(tid, key, ckey))
    t.load_content_records()
    t.content.content_list = [encrypt_content(c, key, i) for i, c in enumerate(contents)]
    if fakesign:
        t.fakesign()
    return t


def write_wad(tmp_path: Path, filename: str = "test.wad", **kw) -> Path:
    p = tmp_path / filename
    p.write_bytes(make_title(**kw).dump_wad())
    return p


WII_US = Settings(console_type="wii", console_region="NTSC-U")
VWII_US = Settings(console_type="vwii", console_region="NTSC-U")
MINI_PAL = Settings(console_type="mini", console_region="PAL")


# --------------------------------------------------------------------------- inspect
def test_inspect_channel(tmp_path):
    info = W.inspect_wad(write_wad(tmp_path))
    assert info.title_id == "0001000157414141"
    assert info.title_ascii == "WAAA"
    assert info.title_type == W.T_WIIWARE
    assert info.name == "Test Channel"
    assert info.region == "NTSC-U"
    assert info.required_ios == 58
    assert info.num_contents == 2
    assert info.hashes_ok is True
    assert info.fakesigned and info.signature_status == "fakesigned"
    assert not info.signed
    assert info.platform == "wii"
    assert info.pretty_id == "00010001-57414141"


def test_inspect_skip_hash_and_corruption(tmp_path):
    p = write_wad(tmp_path)
    data = bytearray(p.read_bytes())
    data[-0x50] ^= 0xFF          # flip a byte in the last content
    p.write_bytes(bytes(data))
    assert W.inspect_wad(p, verify_hashes=False).hashes_ok is None
    info = W.inspect_wad(p)
    assert info.hashes_ok is False and info.bad_contents == [1]
    risks = W.classify_risk(info, WII_US)
    assert W.is_blocked(risks) and risks[0].code == "corrupt"


def test_not_a_wad(tmp_path):
    p = tmp_path / "x.wad"
    p.write_bytes(b"hello" * 100)
    with pytest.raises(W.WadError):
        W.inspect_wad(p)


@pytest.mark.parametrize("tid,expected", [
    ("0000000100000001", W.T_BOOT2), ("0000000100000002", W.T_SYSTEM_MENU),
    ("000000010000003a", W.T_IOS), ("0000000100000100", W.T_BC), ("0000000100000101", W.T_MIOS),
    ("0000000700000050", W.T_IOS), ("0001000146424345", W.T_VC), ("0001000157414141", W.T_WIIWARE),
    ("0001000148414441", W.T_CHANNEL), ("0001000248414241", W.T_SYSTEM_CHANNEL),
    ("0001000448414141", W.T_GAME_CHANNEL), ("0001000552534245", W.T_DLC),
    ("0001000848414b45", W.T_HIDDEN), ("0001000052534245", W.T_DISC),
])
def test_title_types(tid, expected):
    assert W.title_type_of(tid) == expected


def test_system_menu_region():
    assert W.system_menu_region(513) == "NTSC-U"
    assert W.system_menu_region(514) == "PAL"
    assert W.system_menu_region(512) == "NTSC-J"
    assert W.system_menu_region(518) == "NTSC-K"
    assert W.system_menu_region(609) == "NTSC-U"


# --------------------------------------------------------------------------- risks
def _sm(tmp_path, version, vwii=False):
    # real vWii system titles carry 00000001 IDs with the TMD's vWii flag set
    return W.inspect_wad(write_wad(tmp_path, f"sm{version}.wad", tid="0000000100000002",
                                   version=version, ios=80, region=0, name=None,
                                   vwii=int(vwii), fakesign=False))


@pytest.fixture
def nintendo_signed(monkeypatch):
    """Synthetic certificates can't carry Nintendo's signatures; pretend they verify."""
    monkeypatch.setattr(Title, "get_is_signed", lambda self: True)


def test_system_menu_rules(tmp_path, nintendo_signed):
    sm_u = _sm(tmp_path, 513)
    assert sm_u.title_type == W.T_SYSTEM_MENU and sm_u.region == "NTSC-U" and sm_u.signed
    risks = W.classify_risk(sm_u, WII_US)
    assert not W.is_blocked(risks) and any(r.code == "system_menu" for r in risks)
    assert W.is_blocked(W.classify_risk(sm_u, Settings(console_type="wii", console_region="PAL")))
    assert W.is_blocked(W.classify_risk(sm_u, VWII_US))
    assert W.is_blocked(W.classify_risk(sm_u, MINI_PAL))
    assert W.is_blocked(W.classify_risk(sm_u, Settings()))          # profile unset
    with pytest.raises(W.Refused):
        W.ensure_allowed(sm_u, Settings(console_type="wii", console_region="NTSC-J"))
    vsm = _sm(tmp_path, 609, vwii=True)
    assert vsm.platform == "vwii"
    assert W.is_blocked(W.classify_risk(vsm, WII_US))
    assert not W.is_blocked(W.classify_risk(vsm, VWII_US))


def _blocking(info, settings):
    return [r.code for r in W.classify_risk(info, settings) if r.blocks]


def test_system_menu_must_be_signed_43(tmp_path, monkeypatch):
    # a fakesigned / modified Wii Menu of the right region and version is refused
    mod = W.inspect_wad(write_wad(tmp_path, "mod.wad", tid="0000000100000002", version=513,
                                  ios=80, region=1, name=None))
    assert not mod.signed and "modified_menu" in _blocking(mod, WII_US)
    monkeypatch.setattr(Title, "get_is_signed", lambda self: True)
    # a genuine but older menu of the right region (4.1U v449) is a downgrade: refused
    old = _sm(tmp_path, 449)
    assert W.system_menu_region(449) == "NTSC-U" and old.signed
    assert _blocking(old, WII_US) == ["menu_version"]
    # the Wii's 4.3U is not the vWii's 4.3U and vice versa
    assert "menu_version" in _blocking(_sm(tmp_path, 513, vwii=True), VWII_US)
    assert _blocking(_sm(tmp_path, 513), WII_US) == []
    with pytest.raises(W.Refused, match="4.3"):
        W.ensure_allowed(old, WII_US)


def test_menu_ios_stub_and_unsigned_blocked(tmp_path, nintendo_signed):
    def ios(n, ver, **kw):
        return W.inspect_wad(write_wad(tmp_path, f"ios{n}-{ver}.wad", tid=f"00000001{n:08x}",
                                       version=ver, ios=0, region=3, name=None, fakesign=False, **kw))
    # Nintendo-signed stubs of the IOS a Wii Menu runs on
    for n, ver in ((30, 2816), (50, 5120), (60, 6400), (70, 6912)):
        assert _blocking(ios(n, ver), WII_US) == ["stub_menu_ios"], (n, ver)
    # the genuine working versions pass (with a warning)
    for n, ver in ((80, 6944), (70, 6687), (60, 6174)):
        assert _blocking(ios(n, ver), WII_US) == [], (n, ver)
    # other IOS are not menu IOS: stubs and cIOS bases stay installable
    assert _blocking(ios(58, 6176), WII_US) == [] and _blocking(ios(249, 65280), WII_US) == []
    assert W.is_menu_ios("0000000100000050") and W.is_menu_ios("0000000700000050")
    assert not W.is_menu_ios("000000010000003a") and not W.is_menu_ios("0001000148414441")


def test_vwii_menu_ios_must_be_signed(tmp_path):
    # vWii IOS80 as it really is: 00000001-00000050 with the vWii flag; fakesigned -> refused
    v80 = W.inspect_wad(write_wad(tmp_path, "v80.wad", tid="0000000100000050", version=7200,
                                  ios=0, region=3, name=None, vwii=1))
    assert v80.platform == "vwii" and "modified_menu_ios" in _blocking(v80, VWII_US)
    # a re-keyed (so no longer signed) vWii cIOS base is not a menu IOS and stays allowed
    v38 = W.inspect_wad(write_wad(tmp_path, "v38.wad", tid="0000000100000026", version=4380,
                                  ios=0, region=3, name=None, vwii=1, fakesign=False))
    assert not v38.signed and _blocking(v38, VWII_US) == []
    assert W.is_blocked(W.classify_risk(v38, WII_US))


def test_boot2_type_wad_blocked_whatever_its_id(tmp_path):
    info = W.inspect_wad(write_wad(tmp_path))
    assert _blocking(info, WII_US) == []
    info.wad_type = "ib"
    assert _blocking(info, WII_US) == ["boot2"]


def test_fakesigned_forwarder_channel_still_allowed(tmp_path):
    # what ReWii's own forwarder builder produces: a fakesigned 00010001 channel
    info = W.inspect_wad(write_wad(tmp_path, "fwd.wad", tid="0001000155484145", region=3))
    assert info.fakesigned and info.title_type == W.T_CHANNEL
    assert _blocking(info, WII_US) == [] and _blocking(info, VWII_US) == []
    W.ensure_allowed(info, WII_US)
    (tmp_path / "usb").mkdir()
    assert W.stage_wads(DriveLayout(tmp_path / "usb"), [info.path], settings=WII_US)


def test_ensure_allowed_verifies_unchecked_wads(tmp_path):
    p = write_wad(tmp_path, "c.wad")
    quick = W.inspect_wad(p, verify_hashes=False)
    assert quick.hashes_ok is None
    verified, risks = W.ensure_allowed(quick, WII_US)
    assert verified.hashes_ok is True                   # verified on the way through...
    assert quick.hashes_ok is None and verified is not quick   # ...without changing the argument
    assert not W.is_blocked(risks)
    assert W.ensure_allowed(verified, WII_US)[0] is verified   # already checked: nothing to redo
    # damage a content: the quick scan can't see it, ensure_allowed must
    data = bytearray(p.read_bytes())
    data[-500] ^= 0xFF
    p.write_bytes(bytes(data))
    quick = W.inspect_wad(p, verify_hashes=False)
    with pytest.raises(W.Refused, match="damaged"):
        W.ensure_allowed(quick, WII_US)
    # nothing to verify against -> refused rather than waved through
    quick = W.inspect_wad(write_wad(tmp_path, "d.wad"), verify_hashes=False)
    quick.path = None
    with pytest.raises(W.Refused):
        W.ensure_allowed(quick, WII_US)


def test_ios_rules(tmp_path):
    ios = W.inspect_wad(write_wad(tmp_path, "ios.wad", tid="0000000100000026", ios=0, region=3,
                                  name=None))
    assert ios.title_type == W.T_IOS and ios.name == "IOS38" and ios.ios_slot == 38
    assert ios.region == "World" and ios.required_ios == 0
    assert not W.is_blocked(W.classify_risk(ios, WII_US))
    assert W.is_blocked(W.classify_risk(ios, VWII_US))       # Wii IOS on vWii
    vios = W.inspect_wad(write_wad(tmp_path, "vios.wad", tid="0000000100000026", ios=0, region=3,
                                   name=None, vwii=1))
    assert W.is_blocked(W.classify_risk(vios, WII_US))       # vWii IOS on Wii
    assert not W.is_blocked(W.classify_risk(vios, VWII_US))
    ios80 = W.inspect_wad(write_wad(tmp_path, "ios80.wad", tid="0000000100000050", ios=0, region=3,
                                    name=None))
    codes = [r.code for r in W.classify_risk(ios80, WII_US) if r.blocks]
    assert "modified_menu_ios" in codes                        # fakesigned IOS80 = brick risk


def test_boot2_always_blocked(tmp_path):
    info = W.inspect_wad(write_wad(tmp_path, "b2.wad", tid="0000000100000001", ios=0, name=None))
    assert info.wad_type == "ib"
    assert W.is_blocked(W.classify_risk(info, WII_US))


def test_channel_warnings(tmp_path):
    pal = W.inspect_wad(write_wad(tmp_path, "pal.wad", tid="0001000146424350", region=2))
    assert pal.title_type == W.T_VC
    risks = W.classify_risk(pal, WII_US)
    assert not W.is_blocked(risks) and any(r.code == "region_mismatch" for r in risks)
    vkey = W.inspect_wad(write_wad(tmp_path, "vk.wad", ckey=2))
    assert vkey.common_key_index == 2 and vkey.platform == "vwii" and vkey.hashes_ok
    assert W.is_blocked(W.classify_risk(vkey, WII_US))
    assert not W.is_blocked(W.classify_risk(vkey, VWII_US))
    # channels need no console profile
    assert not W.is_blocked(W.classify_risk(W.inspect_wad(write_wad(tmp_path)), Settings()))


def test_unsigned_warning(tmp_path):
    info = W.inspect_wad(write_wad(tmp_path, fakesign=False))
    assert info.signature_status == "invalid"
    assert any(r.code == "bad_signature" for r in W.classify_risk(info, WII_US))


# --------------------------------------------------------------------------- library / staging
@pytest.mark.skipif(sys.platform == "win32", reason="needs a source file name Windows can't create")
def test_scan_stage_list_unstage(tmp_path):
    lib = tmp_path / "lib"
    (lib / "sub").mkdir(parents=True)
    a = write_wad(lib, "A: game?.wad")
    write_wad(lib / "sub", "b.wad", tid="0001000157424242")
    (lib / "junk.wad").write_bytes(b"nope")
    res = W.scan_wads(lib)
    assert len(res.wads) == 2 and len(res.errors) == 1

    layout = DriveLayout(tmp_path / "usb")
    layout.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    events = []
    staged = W.stage_wads(layout, [a, lib / "sub" / "b.wad"], progress=events.append, settings=WII_US)
    assert [p.name for p in staged] == ["A game.wad", "b.wad"]
    assert events and events[-1].done == events[-1].total
    again = W.stage_wads(layout, [a], settings=WII_US)                # identical -> skipped
    assert again[0] == staged[0]
    other = write_wad(tmp_path, "A game.wad", tid="0001000157434343")
    clash = W.stage_wads(layout, [other], settings=WII_US)
    assert clash[0].name == "A game (2).wad"
    assert len(W.list_staged(layout).wads) == 3
    removed = W.unstage(layout, ["b.wad", clash[0]])
    assert len(removed) == 2 and len(W.list_staged(layout).wads) == 1
    with pytest.raises(W.WadError):
        W.unstage(layout, [a])                       # outside wad/


def test_stage_refuses_blocked(tmp_path):
    sm = write_wad(tmp_path, "sm.wad", tid="0000000100000002", version=514, ios=80, region=2, name=None)
    layout = DriveLayout(tmp_path / "usb")
    layout.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    with pytest.raises(W.Refused):
        W.stage_wads(layout, [sm], settings=WII_US)
    assert not layout.wad.exists() or not any(layout.wad.iterdir())


def test_stage_cancel(tmp_path):
    layout = DriveLayout(tmp_path / "usb")
    layout.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    tok = CancelToken()
    tok.cancel()
    from rewii.core.progress import Cancelled
    with pytest.raises(Cancelled):
        W.stage_wads(layout, [write_wad(tmp_path)], cancel=tok, settings=WII_US)
    assert not list(layout.wad.glob("*.wad"))


# --------------------------------------------------------------------------- pack / unpack


def test_region_changed_korean_console_never_gets_a_system_menu(tmp_path, nintendo_signed):
    sm_u = _sm(tmp_path, 513)
    flagged = Settings(console_type="wii", console_region="NTSC-U", korean_region_changed=True)
    assert "korean_region_changed" in _blocking(sm_u, flagged)
    assert "korean_region_changed" not in _blocking(sm_u, WII_US)


def test_forwarder_game_id_is_lazy_and_cached(tmp_path, monkeypatch):
    from rewii.core import forwarder as F
    info = W.inspect_wad(write_wad(tmp_path))
    calls = []
    with monkeypatch.context() as m:
        m.setattr(F, "channel_game_id", lambda p: (calls.append(p), "RMCE01")[1])
        assert calls == []                                   # inspecting doesn't look for it
        assert info.forwarder_game_id == "RMCE01" == info.forwarder_game_id and calls == [info.path]
    assert W.inspect_wad(write_wad(tmp_path, "b.wad")).forwarder_game_id is None     # an ordinary channel
