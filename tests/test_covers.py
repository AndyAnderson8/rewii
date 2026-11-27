"""Tests for rewii.core.covers (network mocked)."""
from __future__ import annotations

import httpx
import pytest

from rewii.core import config, covers, net
from rewii.core.config import Settings
from rewii.core.layout import DriveLayout
from rewii.core.progress import CancelToken, Cancelled

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 5000
BNR = b"\0" * 0x80 + b"IMET" + b"\0" * 0x6000


@pytest.fixture
def cache(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setattr(config, "cache_dir", lambda: d)
    monkeypatch.setattr(net, "cache_dir", lambda: d)
    yield d
    net.set_client(None)


def serve(available: dict[str, bytes], log: list[str] | None = None):
    def handler(req: httpx.Request):
        url = str(req.url)
        if log is not None:
            log.append(url)
        if url in available:
            return httpx.Response(200, content=available[url])
        return httpx.Response(404)
    net.set_client(httpx.Client(transport=httpx.MockTransport(handler)))


def art(kind, region, gid):
    return covers.ART_URL.format(kind=kind, region=region, game_id=gid)


def test_regions():
    assert covers.cover_regions("RMCE01")[:3] == ["US", "EN", "OTHER"]
    assert covers.cover_regions("RMCJ01")[0] == "JA"
    assert covers.cover_regions("RMCK01")[0] == "KO"
    assert covers.cover_regions("RMCW01")[0] == "ZH"
    assert covers.cover_regions("RMCP01", "FR")[:3] == ["FR", "EN", "US"]
    assert covers.cover_regions("RMCD01")[:2] == ["DE", "EN"]
    assert covers.cover_regions("RMCU01")[0] == "AU"
    r = covers.cover_regions("RMCP01")
    assert len(r) == len(set(r))


def test_fetch_writes_gx_wiiflow_and_cache(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
    (lay.root / "wiiflow").mkdir(parents=True)
    serve({
        art("cover3D", "US", "RMCE01"): PNG,
        art("cover", "EN", "RMCE01"): PNG,          # US missing -> EN
        art("coverfull", "US", "RMCE01"): PNG,      # HQ missing -> LQ
        art("disccustom", "US", "RMCE01"): PNG,     # disc -> disccustom
    })
    seen = []
    rep = covers.fetch_covers(lay, ["rmce01", "ZZZE99"],
                              ["cover3D", "cover", "coverfullHQ", "disc"], Settings(),
                              progress=seen.append, workers=2)
    assert rep.results["RMCE01"] == {"cover3D": "ok", "cover": "ok", "coverfullHQ": "ok",
                                     "disc": "ok"}
    assert set(rep.results["ZZZE99"].values()) == {"missing"}
    assert rep.missing_ids() == ["ZZZE99"]
    assert (lay.gx_images / "RMCE01.png").read_bytes() == PNG
    assert (lay.gx_images / "2D" / "RMCE01.png").exists()
    assert (lay.gx_images / "full" / "RMCE01.png").exists()
    assert (lay.gx_images / "disc" / "RMCE01.png").exists()
    assert (lay.wiiflow / "covers" / "RMCE01.png").exists()
    assert (lay.wiiflow / "boxcovers" / "RMCE01.png").exists()
    assert covers.cached_cover_path("RMCE01", "cover3D") is not None
    assert covers.cached_cover_path("ZZZE99", "cover3D") is None
    assert seen[-1].done == seen[-1].total == 8
    st = covers.cover_status(lay, "RMCE01")
    assert st["cover3D"] and st["disc"] and st["wiiflow_box"] and st["cached"]
    assert not covers.cover_status(lay, "ZZZE99")["cover"]


def test_skip_existing_and_use_cache_offline(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
    lay.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    serve({art("cover3D", "US", "RMCE01"): PNG})
    covers.fetch_covers(lay, ["RMCE01"], ["cover3D"], Settings(), wiiflow=False)
    rep = covers.fetch_covers(lay, ["RMCE01"], ["cover3D"], Settings(), wiiflow=False)
    assert rep.results["RMCE01"]["cover3D"] == "skipped"
    # new drive, no network: served from the local cache
    lay2 = DriveLayout(tmp_path / "usb2")
    lay2.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    log: list[str] = []
    serve({}, log)
    rep = covers.fetch_covers(lay2, ["RMCE01"], ["cover3D"], Settings(), wiiflow=False)
    assert rep.results["RMCE01"]["cover3D"] == "ok" and log == []
    assert (lay2.gx_images / "RMCE01.png").exists()


def test_placeholder_and_network_error(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
    lay.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    serve({art("cover", "US", "RMCE01"): b"\x89PNG\r\n\x1a\n" + b"\0" * 100})
    rep = covers.fetch_covers(lay, ["RMCE01"], ["cover"], Settings(), wiiflow=False)
    assert rep.results["RMCE01"]["cover"] == "missing"

    def boom(req):
        raise httpx.ConnectError("offline")
    net.set_client(httpx.Client(transport=httpx.MockTransport(boom)))
    rep = covers.fetch_covers(lay, ["RSBE01"], ["cover"], Settings(), wiiflow=False)
    assert rep.results["RSBE01"]["cover"] == "error"
    assert "RSBE01/cover" in rep.errors


def test_cancel(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
    lay.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    serve({})
    tok = CancelToken()
    tok.cancel()
    with pytest.raises(Cancelled):
        covers.fetch_covers(lay, ["RMCE01", "RSBE01"], ["cover"], Settings(), cancel=tok)


def test_custom_banner(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
    (lay.root / "wiiflow").mkdir(parents=True)
    log: list[str] = []
    serve({covers.BANNER_URL.format(id="GAL"): BNR}, log)
    p = covers.download_custom_banner(lay, "GALE01")
    assert p == lay.usbloadergx / "cache_bnr" / "GALE01.bnr"
    assert p.read_bytes() == BNR
    assert (lay.wiiflow / "custom_banners" / "GALE01.bnr").exists()
    assert log[0].endswith("GALE01.bnr") and log[1].endswith("GAL.bnr")
    assert covers.download_custom_banner(lay, "RMCE01") is None


def test_remove_covers(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
    lay.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    serve({art("cover3D", "US", "RMCE01"): PNG, art("cover", "US", "RMCE01"): PNG})
    covers.fetch_covers(lay, ["RMCE01"], ["cover3D", "cover"], Settings(), wiiflow=False)
    assert covers.remove_covers(lay, "RMCE01") == 2
    assert covers.best_cached_cover("RMCE01") is not None


# --------------------------------------------------------------------------- regressions
def test_throttling_is_an_error_not_missing(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
    lay.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in

    def handler(req):          # one region is throttled, every other one says "not found"
        return httpx.Response(429 if "/US/" in str(req.url) else 404)
    net.set_client(httpx.Client(transport=httpx.MockTransport(handler)))
    rep = covers.fetch_covers(lay, ["RMCE01"], ["cover"], Settings(), wiiflow=False)
    assert rep.results["RMCE01"]["cover"] == "error" and "429" in rep.errors["RMCE01/cover"]
    serve({})                  # plain 404 everywhere is still "missing"
    rep = covers.fetch_covers(lay, ["RMCE01"], ["cover"], Settings(), wiiflow=False)
    assert rep.results["RMCE01"]["cover"] == "missing" and not rep.errors
    # a cover found in a later region wins even if an earlier region failed
    def handler2(req):
        if "/US/" in str(req.url):
            return httpx.Response(500)
        return httpx.Response(200, content=PNG) if "/EN/" in str(req.url) else httpx.Response(404)
    net.set_client(httpx.Client(transport=httpx.MockTransport(handler2)))
    rep = covers.fetch_covers(lay, ["RMCE01"], ["cover"], Settings(), wiiflow=False)
    assert rep.results["RMCE01"]["cover"] == "ok"


def test_broken_cover_on_drive_is_replaced(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
    lay.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    serve({art("cover3D", "US", "RMCE01"): PNG})
    target = covers.gx_cover_path(lay, "RMCE01", "cover3D")
    target.parent.mkdir(parents=True)
    for junk in (b"", b"<html>error</html>" * 100, PNG[:500]):
        target.write_bytes(junk)
        rep = covers.fetch_covers(lay, ["RMCE01"], ["cover3D"], Settings(), wiiflow=False)
        assert rep.results["RMCE01"]["cover3D"] == "ok" and target.read_bytes() == PNG
    # a damaged cache copy isn't reused either
    covers.cache_path("RMCE01", "cover3D").write_bytes(b"junk")
    target.unlink()
    rep = covers.fetch_covers(lay, ["RMCE01"], ["cover3D"], Settings(), wiiflow=False)
    assert target.read_bytes() == PNG and covers.cache_path("RMCE01", "cover3D").read_bytes() == PNG


def test_invalid_game_ids_never_reach_urls_or_paths(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
    lay.root.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    log: list[str] = []
    serve({}, log)
    bad = ["../../", "/A/B/C", "RM?E01", "??????", "A&B=C1"]
    rep = covers.fetch_covers(lay, bad, ["cover"], Settings(), wiiflow=False)
    assert all(rep.results[g.upper()]["cover"] == "error" for g in bad) and log == []
    assert covers.download_cover("../../", "cover") == (None, "")
    assert covers.download_custom_banner(lay, "../../x") is None and log == []
    outside = tmp_path / "victim.png"
    outside.write_bytes(PNG)
    for g in bad + ["../victim", str(tmp_path / "victim")]:
        for p in (covers.gx_cover_path(lay, g, "cover"), covers.cache_path(g, "cover"),
                  covers.wiiflow_cover_path(lay, g, "cover")):
            assert ".." not in p.parts and p.name.endswith(".png") and "/" not in p.stem
            assert p.is_relative_to(lay.root) or p.is_relative_to(cache)
            assert p.resolve() != outside
        assert covers.remove_covers(lay, g) == 0
        assert not any(covers.cover_status(lay, g).values())
    assert outside.exists()
