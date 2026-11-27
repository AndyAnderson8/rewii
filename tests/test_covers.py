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
    serve({art("cover3D", "US", "RMCE01"): PNG})
    covers.fetch_covers(lay, ["RMCE01"], ["cover3D"], Settings(), wiiflow=False)
    rep = covers.fetch_covers(lay, ["RMCE01"], ["cover3D"], Settings(), wiiflow=False)
    assert rep.results["RMCE01"]["cover3D"] == "skipped"
    # new drive, no network: served from the local cache
    lay2 = DriveLayout(tmp_path / "usb2")
    log: list[str] = []
    serve({}, log)
    rep = covers.fetch_covers(lay2, ["RMCE01"], ["cover3D"], Settings(), wiiflow=False)
    assert rep.results["RMCE01"]["cover3D"] == "ok" and log == []
    assert (lay2.gx_images / "RMCE01.png").exists()


def test_placeholder_and_network_error(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
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
    serve({art("cover3D", "US", "RMCE01"): PNG, art("cover", "US", "RMCE01"): PNG})
    covers.fetch_covers(lay, ["RMCE01"], ["cover3D", "cover"], Settings(), wiiflow=False)
    assert covers.remove_covers(lay, "RMCE01") == 2
    assert covers.best_cached_cover("RMCE01") is not None
