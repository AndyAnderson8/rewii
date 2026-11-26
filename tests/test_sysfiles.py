import hashlib
import io
import json
import zipfile

import httpx
import pytest

from rewii.core import config, net
from rewii.core import sysfiles as S
from rewii.core.layout import DriveLayout


@pytest.fixture
def lay(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(S, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(net, "cache_dir", lambda: tmp_path / "cache")
    yield DriveLayout(tmp_path / "usb")
    net.set_client(None)


def serve(routes):
    def handler(req):
        body = routes.get(str(req.url))
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body if isinstance(body, bytes) else json.dumps(body).encode())
    net.set_client(httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True))


def _zip(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return b.getvalue()


HACKS = "[Block Disc Updates]\nmaxversion=518\nminversion=512\namount=1\nhash=0x2c030000\npatch=0x60000000\n"
CUSTOM = "[Clock fix]\nmaxversion=610\nminversion=608\namount=1\nhash=0x7c0802a6\npatch=0x60000000\n"


def test_hackmii_pinned(lay, monkeypatch):
    z = _zip({"hackmii_installer_v1.2/boot.elf": b"ELF", "hackmii_installer_v1.2/README.txt": b"hi"})
    monkeypatch.setattr(S, "HACKMII_SHA256", hashlib.sha256(z).hexdigest())
    serve({S.HACKMII_URL: z})
    d = S.stage_hackmii(lay)
    assert (d / "boot.elf").read_bytes() == b"ELF" and "<ahb_access/>" in (d / "meta.xml").read_text()


def test_pinned_mismatch_refused(lay):
    serve({S.HACKMII_URL: _zip({"x": b"tampered"})})
    with pytest.raises(S.SysFileError):
        S.stage_hackmii(lay)


def test_d2xl_rejects_paths_outside_apps(lay, monkeypatch):
    z = _zip({"evil.txt": b"x"})
    monkeypatch.setattr(S, "D2XL_MINI_SHA256", hashlib.sha256(z).hexdigest())
    serve({S.D2XL_MINI_URL: z})
    with pytest.raises(S.SysFileError):
        S.stage_d2xl_mini(lay)


def test_priiloader_hacks_update_keeps_custom(lay):
    url = "https://example.invalid/hacks_hash.ini"
    serve({S.PRIILOADER_RELEASE_API: {"tag_name": "0.10.0", "assets": [
        {"name": "hacks_hash.ini", "browser_download_url": url}]}, url: HACKS.encode()})
    p, tag = S.update_priiloader_hacks(lay)
    assert tag == "0.10.0" and S.hack_names(p.read_text()) == ["Block Disc Updates"]
    S.add_custom_hack(lay, CUSTOM)
    S.update_priiloader_hacks(lay)
    assert S.hack_names(p.read_text()) == ["Block Disc Updates", "Clock fix"]
    with pytest.raises(S.SysFileError):
        S.add_custom_hack(lay, "[bad]\npatch=nothex\n")


def test_bootmii_ini(tmp_path):
    (tmp_path / "bootmii").mkdir()
    (tmp_path / "bootmii" / "bootmii.ini").write_text("# comment\nVIDEO=NTSC\nAUTOBOOT=OFF\n")
    S.write_bootmii_ini(tmp_path, {"AUTOBOOT": "SYSMENU", "BOOTDELAY": "1"})
    assert S.read_bootmii_ini(tmp_path) == {"VIDEO": "NTSC", "AUTOBOOT": "SYSMENU", "BOOTDELAY": "1"}
    with pytest.raises(S.SysFileError):
        S.write_bootmii_ini(tmp_path, {"AUTOBOOT": "EVIL"})


def test_update_check(lay):
    assert S.check_for_update("0.1.0") is None          # no repo configured
    serve({"https://api.github.com/repos/o/r/releases/latest": {"tag_name": "v0.2.0", "html_url": "u"}})
    u = S.check_for_update("0.1.0", "o/r")
    assert u.newer and u.latest == "0.2.0"
    assert not S.check_for_update("0.2.0", "o/r").newer


def test_config_files_written_with_exact_line_endings(lay, tmp_path):
    (tmp_path / "bootmii").mkdir()
    p = S.write_bootmii_ini(tmp_path, {"AUTOBOOT": "SYSMENU"})
    assert p.read_bytes() == b"AUTOBOOT=SYSMENU\r\n"
    url = "https://example.invalid/hacks_hash.ini"
    serve({S.PRIILOADER_RELEASE_API: {"tag_name": "1", "assets": [
        {"name": "hacks_hash.ini", "browser_download_url": url}]}, url: HACKS.replace("\n", "\r\n").encode()})
    hp, _ = S.update_priiloader_hacks(lay)
    assert b"\r" not in hp.read_bytes()
