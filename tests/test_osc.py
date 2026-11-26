"""Tests for rewii.core.osc (network mocked)."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import httpx
import pytest

from rewii.core import config, net, osc
from rewii.core.layout import DriveLayout
from rewii.core.osc import App, OSCError, plan_archive


@pytest.fixture
def cache(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setattr(config, "cache_dir", lambda: d)
    monkeypatch.setattr(net, "cache_dir", lambda: d)
    yield d
    net.set_client(None)


def make_zip(files: dict[str, bytes]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, data in files.items():
            z.writestr(n, data)
    return b.getvalue()


META = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<app version="1"><name>USB Loader GX</name><coder>blackb0x</coder>
<version>4.0 r1283</version><short_description>Loader</short_description></app>"""

GX_ZIP = make_zip({"apps/usbloader_gx/boot.dol": b"DOL" * 100,
                   "apps/usbloader_gx/meta.xml": META})
NIN_ZIP = make_zip({"apps/Nintendont/boot.dol": b"N" * 50,
                    "apps/Nintendont/meta.xml": b"<app><name>Nintendont</name>"
                                                b"<version>Newer than 4.431</version></app>",
                    "controllers/057E_0337.ini": b"ini"})


def api_entry(slug, name, version, zipdata, subdirs, platforms=("wii", "vwii", "wii_mini"),
              category="utilities"):
    return {
        "slug": slug, "name": name, "author": "someone", "version": version,
        "category": category,
        "description": {"short": f"{name} short", "long": f"{name} long text"},
        "assets": {"archive": {"url": f"https://hbb1.oscwii.org/api/contents/{slug}/{slug}.zip",
                               "hash": hashlib.md5(zipdata).hexdigest(), "size": len(zipdata)},
                   "icon": {"url": f"https://hbb1.oscwii.org/api/contents/{slug}/icon.png"}},
        "flags": [], "package_type": "dol", "supported_platforms": list(platforms),
        "subdirectories": subdirs, "uncompressed_size": 1, "downloads": 5,
    }


INDEX = [
    api_entry("usbloader_gx", "USB Loader GX", "4.0 r1283", GX_ZIP,
              ["/apps/usbloader_gx", "/apps/usbloader_gx/language"]),
    api_entry("Nintendont", "Nintendont", "Newer than 4.431", NIN_ZIP,
              ["/controllers", "/apps/Nintendont"]),
    api_entry("d2x-cios-installer", "d2x cIOS installer", "3.1", GX_ZIP,
              ["/apps/d2x-cios-installer"], platforms=("wii",)),
    api_entry("snes9xgx", "Snes9x GX", "4.5", GX_ZIP, ["/apps/snes9xgx"],
              category="emulators"),
]


def serve(log=None, zips=None):
    zips = zips or {"usbloader_gx": GX_ZIP, "Nintendont": NIN_ZIP,
                    "d2x-cios-installer": GX_ZIP, "snes9xgx": GX_ZIP}

    def handler(req):
        u = str(req.url)
        if log is not None:
            log.append(u)
        if u == osc.OSC_API:
            return httpx.Response(200, json=INDEX)
        for slug, data in zips.items():
            if u.endswith(f"/{slug}/{slug}.zip"):
                return httpx.Response(200, content=data)
        return httpx.Response(404)
    net.set_client(httpx.Client(transport=httpx.MockTransport(handler)))


def test_index_and_search(cache):
    log: list[str] = []
    serve(log)
    apps = osc.fetch_index()
    assert len(apps) == 4 and osc.fetch_index() and log.count(osc.OSC_API) == 1
    gx = osc.find_app(apps, "USBLOADER_GX")
    assert gx.archive_hash == hashlib.md5(GX_ZIP).hexdigest()
    assert gx.folder == "usbloader_gx" and gx.file_size == len(GX_ZIP)
    nin = osc.find_app(apps, "Nintendont")
    assert nin.app_folders == ["Nintendont"]
    assert [a.slug for a in osc.search(apps, "loader")][0] == "usbloader_gx"
    assert [a.slug for a in osc.search(apps, "long text", category="emulators")] == ["snes9xgx"]
    assert "d2x-cios-installer" not in [a.slug for a in osc.search(apps, platform="vwii")]
    assert "d2x-cios-installer" not in [a.slug for a in osc.search(apps, platform="mini")]
    assert len(osc.search(apps)) == 4


def test_offline_without_cache_raises(cache):
    def boom(req):
        raise httpx.ConnectError("offline")
    net.set_client(httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(net.NetError):
        osc.fetch_index()


def test_plan_archive_layouts():
    assert plan_archive(["apps/x/boot.dol", "wiiflow/a.ini"], "x") == {
        "apps/x/boot.dol": "apps/x/boot.dol", "wiiflow/a.ini": "wiiflow/a.ini"}
    assert plan_archive(["Wrap-1.0/apps/x/boot.dol", "Wrap-1.0/data/y"], "x") == {
        "Wrap-1.0/apps/x/boot.dol": "apps/x/boot.dol", "Wrap-1.0/data/y": "data/y"}
    assert plan_archive(["boot.dol", "meta.xml"], "foo") == {
        "boot.dol": "apps/foo/boot.dol", "meta.xml": "apps/foo/meta.xml"}
    assert plan_archive(["bar/boot.dol", "bar/icon.png"], "foo") == {
        "bar/boot.dol": "apps/bar/boot.dol", "bar/icon.png": "apps/bar/icon.png"}
    assert plan_archive(["Apps/x/boot.dol", "dir/"], "x") == {"Apps/x/boot.dol": "apps/x/boot.dol"}
    for bad in (["../evil"], ["/abs/x"], ["C:/x"], ["apps/x/../../../e"], ["apps\\..\\..\\e"],
                ["apps/x/a:b"]):
        with pytest.raises(OSCError):
            plan_archive(bad, "x")


def test_install_update_remove(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
    serve()
    apps = osc.fetch_index()
    gx = osc.find_app(apps, "usbloader_gx")
    events = []
    res = osc.install_app(lay, gx, events.append)
    assert (lay.apps / "usbloader_gx" / "boot.dol").exists()
    assert len(res.files) == 2 and events[-1].done == events[-1].total == 1000
    nin = osc.find_app(apps, "Nintendont")
    osc.install_app(lay, nin)
    assert (lay.root / "controllers" / "057E_0337.ini").exists()

    inst = {i.folder: i for i in osc.installed_apps(lay)}
    assert inst["usbloader_gx"].name == "USB Loader GX"
    assert inst["usbloader_gx"].version == "4.0 r1283"
    assert inst["usbloader_gx"].coder == "blackb0x"
    assert inst["usbloader_gx"].bootable and inst["usbloader_gx"].slug == "usbloader_gx"
    assert osc.update_available(inst.values(), apps) == {}

    newer = App(**{**gx.__dict__, "version": "4.1", "archive_hash": "0" * 32})
    upd = osc.update_available(inst.values(), [newer, nin])
    assert list(upd) == ["usbloader_gx"]

    assert osc.remove_app(lay, "usbloader_gx")
    assert not (lay.apps / "usbloader_gx").exists()
    assert not osc.remove_app(lay, "usbloader_gx")
    with pytest.raises(OSCError):
        osc.remove_app(lay, "../x")


def test_version_compare_without_record(tmp_path):
    lay = DriveLayout(tmp_path / "usb")
    d = lay.apps / "wiixplorer"
    d.mkdir(parents=True)
    (d / "boot.dol").write_bytes(b"x")
    (d / "meta.xml").write_text("<app><name>WiiXplorer</name><version>r258</version>"
                                "<coder>Dimok & co</coder></app>")   # invalid XML (&)
    inst = osc.installed_apps(lay)
    assert inst[0].name == "WiiXplorer" and inst[0].coder == "Dimok & co"
    a = App("wiixplorer", "WiiXplorer", version="r259", subdirectories=["/apps/wiixplorer"])
    assert list(osc.update_available(inst, [a])) == ["wiixplorer"]
    a.version = "R259 "
    inst[0].version = "r259"
    assert osc.update_available(inst, [a]) == {}


def test_nested_detection(tmp_path):
    lay = DriveLayout(tmp_path / "usb")
    for p in ("apps/apps/foo", "apps/bar/apps/baz", "rootapp"):
        d = lay.root / p
        d.mkdir(parents=True)
        (d / "boot.dol").write_bytes(b"x")
    (lay.root / "rootapp" / "meta.xml").write_text("<app><name>R</name></app>")
    inst = osc.installed_apps(lay)
    nested = sorted(i.folder for i in inst if i.nested)
    assert nested == ["baz", "foo"]
    bar = next(i for i in inst if i.folder == "bar")
    assert not bar.bootable
    mis = sorted(p.name for p in osc.misplaced_apps(lay))
    assert mis == ["baz", "foo", "rootapp"]


def test_bad_hash_rejected(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
    serve(zips={"usbloader_gx": b"PK tampered"})
    gx = App.from_api(INDEX[0])
    with pytest.raises(osc.OSCError):
        osc.install_app(lay, gx)
    assert not (lay.apps / "usbloader_gx").exists()


def test_starter_kit(tmp_path, cache):
    lay = DriveLayout(tmp_path / "usb")
    serve()
    apps = osc.fetch_index()
    assert {i.slug for i in osc.starter_kit("vwii")} >= {"d2x-cios-installer-vwii"}
    assert "d2x-cios-installer" not in {i.slug for i in osc.starter_kit("vwii")}
    assert all(not i.essential for i in osc.starter_kit("", "recommended"))
    with pytest.raises(ValueError):
        osc.starter_kit("", "bogus")
    rep = osc.install_starter_kit(lay, "wii", ["usbloader_gx", "Nintendont",
                                               "d2x-cios-installer", "yawmME"], apps=apps)
    assert sorted(rep.installed) == ["Nintendont", "d2x-cios-installer", "usbloader_gx"]
    assert rep.not_found == ["yawmME"] and not rep.failed
    rep2 = osc.install_starter_kit(lay, "wii", ["usbloader_gx"], apps=apps)
    assert rep2.installed == []          # already current
    rec = json.loads((lay.rewii / osc.RECORD_NAME).read_text())
    assert rec["usbloader_gx"]["slug"] == "usbloader_gx"


def test_starter_kit_slugs_unique():
    slugs = [i.slug for i in osc.STARTER_KIT]
    assert len(slugs) == len(set(slugs))


def test_folder_without_boot_is_not_an_update(tmp_path):
    lay = DriveLayout(tmp_path / "usb")
    (lay.apps / "usbloader_gx" / "images").mkdir(parents=True)
    a = App("usbloader_gx", "USB Loader GX", version="4.0", subdirectories=["/apps/usbloader_gx"])
    assert osc.update_available(osc.installed_apps(lay), [a]) == {}
