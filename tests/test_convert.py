"""Tests for rewii.core.convert using a fake converter script (no network, no real nodtool)."""
from __future__ import annotations

import hashlib
import os
import struct
import sys
import threading
from pathlib import Path

import httpx
import pytest

from rewii.core import convert, net
from rewii.core.disc import GC_MAGIC
from rewii.core.progress import Cancelled, CancelToken

pytestmark = pytest.mark.skipif(os.name == "nt", reason="fake tool is a shebang script")

OUT_SIZE = 1 << 20


def gc_header() -> bytes:
    h = bytearray(0x440)
    h[:6] = b"GTEST1"
    h[0x1C:0x20] = struct.pack(">I", GC_MAGIC)
    h[0x20:0x30] = b"Test Game"
    return bytes(h)


FAKE_TOOL = r'''#!{python}
import sys, time, os
args = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "ok")
src, out = args[-2], args[-1]
if mode == "fail":
    print("error: disc image is corrupt", file=sys.stderr)
    sys.exit(3)
with open(out, "wb") as f:
    head = bytearray({header!r})
    if mode == "nkit":
        head[0x200:0x204] = b"NKIT"
    f.write(head)
    for _ in range(16):
        f.write(bytes({size} // 16 - (len(head) if _ == 0 else 0)))
        f.flush()
        if mode == "slow":
            time.sleep(0.5)
with open(out + ".args", "w") as f:
    f.write("\n".join(args))
'''


@pytest.fixture
def fake_tool(tmp_path, monkeypatch):
    p = tmp_path / "tools" / "nodtool-fake"
    p.parent.mkdir()
    p.write_text(FAKE_TOOL.format(python=sys.executable, header=gc_header(), size=OUT_SIZE))
    p.chmod(0o755)
    # the cached tool is only run if it matches the digest recorded when it was downloaded
    convert._digest_file(p).write_text(hashlib.sha256(p.read_bytes()).hexdigest())
    monkeypatch.setattr(convert, "cached_tool_path", lambda: p)
    monkeypatch.setattr(convert, "POLL", 0.05)
    return p


def rvz(tmp_path: Path, size: int = OUT_SIZE) -> Path:
    p = tmp_path / "game.rvz"
    head = bytearray(0x48)
    head[:4] = b"RVZ\x01"
    head[0x24:0x2C] = struct.pack(">Q", size)
    p.write_bytes(bytes(head) + bytes(100))
    return p


def test_to_iso_with_tool(fake_tool, tmp_path):
    events = []
    out = convert.to_iso(rvz(tmp_path), tmp_path / "out" / "game.iso", progress=events.append)
    assert out.stat().st_size == OUT_SIZE
    assert events[-1].done == events[-1].total == OUT_SIZE
    args = Path(str(tmp_path / "out" / ".rewii-tmp-game.iso") + ".args").read_text().split("\n")
    assert args[:2] == ["--no-color", "convert"]
    assert args[-1].endswith(".rewii-tmp-game.iso")
    assert not (tmp_path / "out" / ".rewii-tmp-game.iso").exists()


def test_tool_failure_cleans_up(fake_tool, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "fail")
    with pytest.raises(convert.ConvertError, match="corrupt"):
        convert.to_iso(rvz(tmp_path), tmp_path / "game.iso")
    assert not list(tmp_path.glob("game*.iso"))


def test_cancel_terminates_and_cleans_up(fake_tool, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "slow")
    tok = CancelToken()
    threading.Timer(0.4, tok.cancel).start()
    with pytest.raises(Cancelled):
        convert.to_iso(rvz(tmp_path), tmp_path / "game.iso", cancel=tok)
    assert not list(tmp_path.glob("game*.iso"))


def test_nkit_refused(fake_tool, tmp_path, monkeypatch):
    nk = tmp_path / "x.nkit.iso"
    h = bytearray(gc_header())
    h[0x200:0x204] = b"NKIT"
    nk.write_bytes(bytes(h))
    with pytest.raises(convert.ConvertError, match="NKit"):
        convert.to_iso(nk, tmp_path / "x.iso")
    monkeypatch.setenv("FAKE_MODE", "nkit")
    with pytest.raises(convert.ConvertError, match="NKit"):
        convert.to_iso(rvz(tmp_path), tmp_path / "game.iso")
    assert not (tmp_path / "game.iso").exists()


def test_wdf_needs_wit(tmp_path, monkeypatch):
    monkeypatch.setattr(convert.shutil, "which", lambda name: None)
    p = tmp_path / "g.wdf"
    p.write_bytes(b"WII\x01DISC" + bytes(0x400))
    with pytest.raises(convert.ConvertError, match="wit"):
        convert.to_iso(p, tmp_path / "g.iso")


def test_plain_iso_is_copied(tmp_path):
    src = tmp_path / "a.iso"
    src.write_bytes(gc_header() + bytes(5000))
    out = convert.to_iso(src, tmp_path / "b.iso")
    assert out.read_bytes() == src.read_bytes()


def test_unknown_file(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00" * 100)
    with pytest.raises(convert.ConvertError):
        convert.to_iso(p, tmp_path / "x.iso")


def test_expected_sizes(tmp_path):
    assert convert.expected_iso_size(rvz(tmp_path, 1459978240)) == 1459978240
    g = tmp_path / "g.gcz"
    g.write_bytes(b"\x01\xc0\x0b\xb1" + struct.pack("<IQQII", 0, 10, 4699979776, 0x4000, 1) + bytes(32))
    assert convert.expected_iso_size(g) == 4699979776


def test_commands():
    src, out = Path("in.rvz"), Path("out.iso")
    assert convert._command(convert.Tool("dolphintool", Path("D")), src, out) == [
        "D", "convert", "-f", "iso", "-i", "in.rvz", "-o", "out.iso"]
    assert convert._command(convert.Tool("wit", Path("wit")), src, out)[:3] == ["wit", "copy", "--iso"]


@pytest.mark.parametrize("plat,machine,expected", [
    ("win32", "AMD64", ("windows", "x86_64")), ("darwin", "arm64", ("macos", "arm64")),
    ("linux", "aarch64", ("linux", "arm64")), ("linux", "x86_64", ("linux", "x86_64")),
])
def test_host_platform(monkeypatch, plat, machine, expected):
    monkeypatch.setattr(convert.sys, "platform", plat)
    monkeypatch.setattr(convert._platform, "machine", lambda: machine)
    assert convert.host_platform() == expected
    assert expected in convert.NOD_ASSETS


def test_ensure_tool_download(tmp_path, monkeypatch):
    blob = b"\x7fELF fake nodtool"
    good = hashlib.sha256(blob).hexdigest()
    monkeypatch.setattr(convert, "tools_dir", lambda: tmp_path)
    monkeypatch.setattr(convert.shutil, "which", lambda name: None)
    monkeypatch.setattr(convert, "host_platform", lambda: ("linux", "x86_64"))
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200, content=blob)

    net.set_client(httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        monkeypatch.setitem(convert.NOD_ASSETS, ("linux", "x86_64"), ("nodtool-linux-x86_64", "0" * 64))
        assert not convert.tool_available()
        with pytest.raises(convert.ConvertError, match="checksum"):
            convert.ensure_tool()
        assert not convert.tool_available() and list(tmp_path.iterdir()) == []
        monkeypatch.setitem(convert.NOD_ASSETS, ("linux", "x86_64"), ("nodtool-linux-x86_64", good))
        p = convert.ensure_tool()
        assert p.read_bytes() == blob and os.access(p, os.X_OK)
        assert convert.tool_available()
        assert seen[-1].endswith(f"/{convert.NOD_VERSION}/nodtool-linux-x86_64")
        n = len(seen)
        assert convert.ensure_tool() == p and len(seen) == n   # cached, no download
    finally:
        net.set_client(None)
