import hashlib
import struct
import zipfile

import pytest

from fakedisc import make_gc_iso
from rewii.core import forwarder as F
from rewii.core import gcforwarder as GC
from rewii.core.channels import build_channel, channel_problem
from rewii.core.config import Settings
from rewii.core.layout import DriveLayout


@pytest.fixture
def certs(monkeypatch):
    monkeypatch.setattr(F, "cert_chain", lambda: bytes(2560))


@pytest.fixture
def nin():
    # any libogc DOL with an argv area works as a stand-in for Nintendont
    return F._asset("ConfUsbLoaderFat.dol") + b"$$Version:6.510\0"


@pytest.fixture
def drive(tmp_path):
    lay = DriveLayout(tmp_path / "usb")
    lay.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    lay.ensure_dirs()
    game = lay.gc_game_file("GC TEST", "GTST01")
    game.parent.mkdir(parents=True)
    make_gc_iso(game)
    return lay, game


def _texts(banner: bytes) -> dict:
    body = banner[0x640:]
    f = GC._u8_files(body)
    o, s = f["banner.bin"]
    raw = GC._imd5_unwrap(body[o:o + s])
    ff = GC._u8_files(raw)
    o2, s2 = ff["banner.brlyt"]
    lyt = raw[o2:o2 + s2]
    out = {}
    _m, _b, _v, size, hsz, nsec = struct.unpack(">4sHHIHH", lyt[:16])
    assert size == len(lyt)
    p = hsz
    for _ in range(nsec):
        kind, ssz = struct.unpack(">4sI", lyt[p:p + 8])
        if kind == b"txt1":
            name = lyt[p + 0x0C:p + 0x1C].split(b"\0")[0].decode()
            ln = struct.unpack(">H", lyt[p + 0x4C:p + 0x4E])[0]
            toff = struct.unpack(">I", lyt[p + 0x58:p + 0x5C])[0]
            out[name] = lyt[p + toff:p + toff + ln].decode("utf-16-be").rstrip("\0")
        p += ssz
    return out


def test_gc_banner_from_disc(drive):
    _lay, game = drive
    gb = GC.read_gc_banner(game)
    assert gb.name == "GC TEST" and gb.company == "TESTCO."
    assert gb.rgba[:4] == bytes((255, 0, 0, 255))


def test_rgb5a3_roundtrip():
    import os
    px = bytes(b if i % 4 != 3 else 255 for i, b in enumerate(os.urandom(8 * 8 * 4)))
    back = GC.rgb5a3_decode(GC.rgb5a3_encode(px, 8, 8), 8, 8)
    assert all(abs(a - b) <= 8 for a, b in zip(px, back))


def test_banner_structure_preserved():
    banner = GC.build_gc_banner("Kirby Air Ride", None, publisher="Nintendo", year="2003",
                                game_id="GKYE01", synopsis="Race!")
    imet = bytearray(banner[0x40:0x640])
    md5 = bytes(imet[0x5F0:0x600])
    imet[0x5F0:0x600] = bytes(16)
    assert hashlib.md5(imet).digest() == md5
    assert imet[0x5C:0x5C + 28].decode("utf-16-be") == "Kirby Air Ride"
    t = _texts(banner)
    assert t["T_name"] == "Kirby Air Ride" and t["T_coder"] == "Nintendo" and t["T_PF"] == "GameCube"
    # same files, same order, same uncompressed sizes as the template
    tpl = F._asset("gc_banner_template.bnr")[0x640:]
    a, b = GC._u8_files(tpl), GC._u8_files(banner[0x640:])
    assert list(a) == list(b)
    for name in ("banner.bin", "icon.bin"):
        ra = GC._imd5_unwrap(tpl[a[name][0]:sum(a[name])])
        rb = GC._imd5_unwrap(banner[0x640:][b[name][0]:sum(b[name])])
        assert len(ra) == len(rb)
    assert tpl[a["sound.bin"][0]:sum(a["sound.bin"])] == banner[0x640:][b["sound.bin"][0]:sum(b["sound.bin"])]


def test_long_text_is_truncated_not_grown():
    banner = GC.build_gc_banner("X" * 300, None, synopsis="y" * 5000)
    t = _texts(banner)
    assert len(t["T_name"]) == 49 and len(t["T_long_descripti"]) < 1200


def test_make_gc_channel(drive, tmp_path, certs, nin):
    lay, game = drive
    (lay.root / "nincfg.bin").write_bytes(GC.build_nin_cfg("/old.iso", "GOLD", None)[:8]
                                          + struct.pack(">III", 1 << 3 | 1 << 5, 0x10, 1) + bytes(528))
    ci = GC.make_gc_channel(game, lay.root, tmp_path / "out", nintendont=nin, publisher="Pub", year="2001")
    assert ci.channel_id == "GTST" and F.verify_wad(ci.path) == (True, "ok")
    assert F.channel_game_id(ci.path) == "GTST01"
    r = F.unpack_wad(ci.path.read_bytes())
    raw = F.read_argv_raw(r["contents"][1])
    a0, cfg = raw.split(b"\0", 1)
    assert a0 == b"usb:/apps/Nintendont/boot.dol"
    magic, ver, config, video, lang = struct.unpack(">5I", cfg[:20])
    assert (magic, ver) == (GC.NIN_MAGIC, GC.NIN_CFG_VERSION)
    assert config & GC.CFG_AUTO_BOOT and config & GC.CFG_USB and config & (1 << 5)   # inherited force-wide
    assert (video, lang) == (0x10, 1)
    assert cfg[20:60].split(b"\0")[0] == b"/games/GC TEST [GTST01]/game.iso"
    assert cfg[536:540] == b"GTST"
    from libWiiPy.title import wad as W
    w = W.WAD()
    w.load(ci.path.read_bytes())
    tmd = w.get_tmd_data()
    assert struct.unpack(">Q", tmd[0x184:0x18C])[0] == GC.IOS58
    assert struct.unpack(">I", tmd[0x1D8:0x1DC])[0] == 3


def test_gc_channel_rules(drive, tmp_path, certs, nin):
    lay, game = drive
    assert channel_problem("gc", 1, "GTST01", True)
    assert channel_problem("gc", 0, "GTST01", False)
    assert channel_problem("gc", 0, "GTST01", True) is None
    outside = make_gc_iso(tmp_path / "elsewhere.iso")
    with pytest.raises(F.ForwarderError):
        GC.make_gc_channel(outside, lay.root, tmp_path / "o", nintendont=nin)
    s = Settings(library_dir=str(tmp_path / "lib"))
    ci = build_channel(game, s, drive_root=lay.root, nintendont=nin)
    assert ci.style == "gamecube"


def test_old_nintendont_refused(drive, monkeypatch):
    lay, _ = drive
    d = lay.app_dir("Nintendont")
    d.mkdir(parents=True)
    (d / "boot.dol").write_bytes(b"x" * 100 + b"$$Version:5.400")
    with pytest.raises(GC.NintendontMissing):
        GC.get_nintendont_dol(lay.root, allow_download=False)
    (d / "boot.dol").write_bytes(b"x" * 100 + b"$$Version:6.510")
    assert GC.get_nintendont_dol(lay.root, allow_download=False).endswith(b"6.510")


def test_osc_repacked_zip_accepted_by_binary_hash(tmp_path):
    from rewii.core import osc
    dol = b"DOLDATA"
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("apps/X/boot.dol", dol)
    app = osc.App(slug="X", name="X", archive_hash="0" * 32, binary_hash=hashlib.md5(dol).hexdigest())
    assert osc._archive_ok(app, z)
    app.binary_hash = "1" * 32
    assert not osc._archive_ok(app, z)


def test_gc_channel_uses_cheat_file(drive, tmp_path, certs, nin):
    lay, game = drive
    (lay.root / "codes").mkdir()
    (lay.root / "codes" / "GTST01.gct").write_bytes(b"\x00\xd0\xc0\xde" * 2)
    ci = GC.make_gc_channel(game, lay.root, tmp_path / "out", nintendont=nin)
    cfg = F.read_argv_raw(F.unpack_wad(ci.path.read_bytes())["contents"][1]).split(b"\0", 1)[1]
    config = struct.unpack(">I", cfg[8:12])[0]
    assert config & GC.CFG_CHEATS and config & GC.CFG_CHEAT_PATH
    assert cfg[275:300].split(b"\0")[0] == b"/codes/GTST01.gct"
