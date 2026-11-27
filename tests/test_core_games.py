import shutil

import pytest

from fakedisc import imet_banner, make_ciso, make_gc_iso, make_wii_iso
from rewii.core import disc as D
from rewii.core import forwarder as F
from rewii.core import games as G
from rewii.core.config import Settings
from rewii.core.layout import DriveLayout, game_folder_name, safe_name
from rewii.core.wbfs import SplitWriter, WbfsPartition, write_iso, write_wbfs

CERTS = bytes(2560)       # a stand-in certificate chain (the builders only embed it)


@pytest.fixture(autouse=True)
def private_cache(tmp_path, monkeypatch):
    """Keep the work folder for joins/conversions out of the real user cache."""
    from rewii.core import config
    monkeypatch.setattr(config, "cache_dir", lambda: tmp_path / "cache")
    (tmp_path / "cache").mkdir()


@pytest.fixture
def wii(tmp_path):
    return make_wii_iso(tmp_path / "game.iso")


@pytest.fixture
def lay(tmp_path):
    l = DriveLayout(tmp_path / "drive")
    l.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    l.ensure_dirs()
    return l


@pytest.fixture
def settings(tmp_path):
    s = Settings(library_dir=str(tmp_path / "lib"))
    s.ensure_library()
    return s


def test_sniff_and_info(wii, tmp_path):
    assert D.sniff_format(wii) == "ISO"
    i = D.disc_info(wii)
    assert (i.game_id, i.title, i.platform, i.region) == ("RTST01", "TEST GAME", "wii", "NTSC-K")
    gc = make_gc_iso(tmp_path / "gc.iso")
    assert D.disc_info(gc).platform == "gc"
    (tmp_path / "x.rvz").write_bytes(b"RVZ\x01" + bytes(100))
    with pytest.raises(D.NeedsConversion):
        D.open_disc(tmp_path / "x.rvz")


def test_partition_decrypt_and_fst(wii):
    with D.open_disc(wii) as d:
        assert D.read_wii_file(d, "opening.bnr") == imet_banner()
        assert D.read_wii_file(d, "data.bin") == b"\x5A" * 70000
        used = D.used_sectors(d)
        assert 3 < sum(used) < 20


def test_wbfs_roundtrip(wii, tmp_path):
    with D.open_disc(wii) as d:
        paths = write_wbfs(d, tmp_path / "RTST01.wbfs")
        used = D.used_sectors(d)
        with D.open_disc(paths[0]) as w:
            assert w.format == "WBFS"
            for s, u in enumerate(used):
                if u:
                    assert w.read(s * D.WII_SECTOR, D.WII_SECTOR) == d.read(s * D.WII_SECTOR, D.WII_SECTOR)
            iso = write_iso(w, tmp_path / "back.iso")[0]
    with D.open_disc(iso) as d2:
        assert D.read_wii_file(d2, "data.bin") == b"\x5A" * 70000


def test_wbfs_split(wii, tmp_path):
    with D.open_disc(wii) as d:
        paths = write_wbfs(d, tmp_path / "RTST01.wbfs", split=3 * 1024 * 1024)
    assert len(paths) >= 2 and paths[1].suffix == ".wbf1"
    assert all(p.stat().st_size <= 3 * 1024 * 1024 for p in paths)
    with D.open_disc(paths[0]) as w:
        assert D.read_wii_file(w, "data.bin") == b"\x5A" * 70000


def test_ciso_reader(wii, tmp_path):
    c = make_ciso(wii, tmp_path / "g.ciso")
    with D.open_disc(c) as r:
        assert r.format == "CISO"
        assert D.read_wii_file(r, "data.bin") == b"\x5A" * 70000


def test_split_writer_part_naming(tmp_path):
    w = SplitWriter(tmp_path / "a.iso", 10, naming="part")
    w.write_at(0, b"x" * 25)
    w.truncate(25)
    w.close()
    assert [p.name for p in w.paths] == ["a.iso", "a.part1.iso", "a.part2.iso"]


def test_verify(wii, tmp_path):
    assert G.verify_game(wii).ok
    # corrupt one byte of encrypted data in the first cluster
    with open(wii, "r+b") as f:
        f.seek(0x50000 + 0x20000 + 0x1000)
        b = f.read(1)
        f.seek(-1, 1)
        f.write(bytes([b[0] ^ 0xFF]))
    r = G.verify_game(wii)
    assert not r.ok and r.bad_clusters == 1


def test_install_scan_remove(wii, lay, tmp_path):
    g = G.install_game(lay, wii, title="Test: Game?")
    assert g.path == lay.wbfs / "Test Game [RTST01]" / "RTST01.wbfs"
    gc = G.install_game(lay, make_gc_iso(tmp_path / "gc.iso"))
    assert gc.path == lay.gc_games / "GC TEST [GTST01]" / "game.iso"
    d2 = G.install_game(lay, make_gc_iso(tmp_path / "gc2.iso", title="Other", disc=1))
    assert d2.path == lay.gc_games / "GC TEST [GTST01]" / "disc2.iso"
    games = G.scan_drive(lay)
    assert {x.key for x in games} == {"RTST01:0", "GTST01:0", "GTST01:1"}
    assert all(not x.problem for x in games)
    assert games[0].title == "Test Game"          # folder title preferred over disc title
    with pytest.raises(G.GameError):
        G.install_game(lay, wii)
    G.remove_game(lay, g)
    assert not (lay.wbfs / "Test Game [RTST01]").exists()


def test_normalize(wii, lay):
    shutil.copy(wii, lay.wbfs / "random name.iso")
    assert G.scan_drive(lay)[0].problem == "misplaced"
    msgs = G.normalize_drive(lay)
    assert len(msgs) == 1
    assert (lay.wbfs / "TEST GAME [RTST01]" / "RTST01.iso").exists()
    assert not G.scan_drive(lay)[0].problem


def test_library(wii, settings, lay):
    g = G.add_to_library(settings, wii)
    assert g.path == settings.wii_games_dir / "TEST GAME [RTST01].wbfs"
    assert [x.game_id for x in G.scan_library(settings)] == ["RTST01"]
    d = G.install_game(lay, g.path)
    G.delete_from_library(settings, g)
    back = G.copy_to_library(settings, d)
    assert back.path.exists()


def test_wbfs_partition_migration(wii, lay, settings, tmp_path):
    # build a tiny raw WBFS partition with one disc in slot 0
    with D.open_disc(wii) as d:
        wf = write_wbfs(d, tmp_path / "one.wbfs")[0]
    img = tmp_path / "part.img"
    data = bytearray(wf.read_bytes())
    data[12] = 1
    img.write_bytes(data)
    p = WbfsPartition(img)
    assert [g.game_id for g in p.games()] == ["RTST01"]
    p.close()
    msgs = G.migrate_wbfs_partition(img, lay, settings)
    assert len(msgs) == 2
    assert G.scan_drive(lay)[0].game_id == "RTST01"


def test_layout_names():
    assert safe_name('A/B:C*?"<>|') == "ABC"
    assert game_folder_name("Mario Kart Wii", "RMCE01") == "Mario Kart Wii [RMCE01]"


# ------------------------------------------------------------------ forwarder
@pytest.fixture
def certs(monkeypatch):
    monkeypatch.setattr("rewii.core.nus.cert_chain", lambda: CERTS)


def test_channel_styles(wii, tmp_path, certs):
    for style in F.STYLES:
        ci = F.make_channel(wii, tmp_path / style, style=style, cover_png=None, cert_chain=CERTS)
        assert F.verify_wad(ci.path) == (True, "ok")
        assert F.channel_game_id(ci.path) == "RTST01"
        assert ci.channel_id == "UTST"
        dol = F.verify_unpack(ci.path.read_bytes())["contents"][1]
        assert F.read_argv(dol) == (["rewii", "intro=0"] if style == "black" else [])
    # the WAD list asks for just the loader: one small partial read, not a second full parse
    assert F.read_wad_content(ci.path, 1) == dol and F.read_wad_content(ci.path, 9) is None
    assert F.read_wad_content(ci.path, 1, max_size=10) is None
    junk = tmp_path / "junk.wad"
    junk.write_bytes(b"not a wad at all" * 10)
    assert F.channel_game_id(junk) is None


def test_channel_refuses_brick_games(tmp_path, certs):
    p = make_wii_iso(tmp_path / "mp9.iso", game_id="SSQE01")
    with pytest.raises(F.BannerBrickRisk):
        F.make_channel(p, tmp_path, cert_chain=CERTS)


def test_channel_rejects_gamecube(tmp_path, certs):
    with pytest.raises(F.ForwarderError):
        F.make_channel(make_gc_iso(tmp_path / "gc.iso"), tmp_path, cert_chain=CERTS)


def test_splash_fits():
    from PIL import Image
    import io
    img = Image.effect_noise((300, 420), 80).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    png = F.splash_png(buf.getvalue())
    assert len(png) <= 32080
    assert Image.open(io.BytesIO(png)).size == (640, 480)


def test_rename_library_and_drive(wii, settings, lay, tmp_path):
    g = G.add_to_library(settings, wii)
    r = G.rename_library_game(settings, g, "New: Name")
    assert r.path.name == "New Name [RTST01].wbfs" and r.path.exists()
    d = G.install_game(lay, r.path)
    r2 = G.rename_game(lay, d, "Other")
    assert r2.path == lay.wbfs / "Other [RTST01]" / "RTST01.wbfs"
    gc = G.add_to_library(settings, make_gc_iso(tmp_path / "gc.iso"))
    r3 = G.rename_library_game(settings, gc, "GC Renamed")
    assert r3.path.parent.name == "GC Renamed [GTST01]"
