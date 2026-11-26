"""Tests for rewii.core.net (network mocked)."""
from __future__ import annotations

import os
import time

import httpx
import pytest

from rewii.core import net


@pytest.fixture
def cache(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setattr(net, "cache_dir", lambda: d)
    yield d
    net.set_client(None)


def serve(body: bytes, status: int = 200, calls: list | None = None):
    def handler(req):
        if calls is not None:
            calls.append(str(req.url))
        return httpx.Response(status, content=body)
    net.set_client(httpx.Client(transport=httpx.MockTransport(handler)))


def is_json_list(p):
    return p.read_bytes().lstrip().startswith(b"[")


def test_cached_never_keeps_invalid_data(cache):
    serve(b"<html>captive portal</html>")
    with pytest.raises(net.NetError, match="invalid"):
        net.cached("https://example.invalid/x.json", "x/x.json", validate=is_json_list)
    assert not list((cache / "x").iterdir())            # nothing cached, no .part left
    serve(b"[1]")
    p = net.cached("https://example.invalid/x.json", "x/x.json", validate=is_json_list)
    assert p.read_bytes() == b"[1]"


def test_cached_keeps_good_copy_when_refresh_is_invalid(cache):
    serve(b"[1]")
    p = net.cached("https://example.invalid/x.json", "x.json", validate=is_json_list)
    old = time.time() - 10 * 86400
    os.utime(p, (old, old))                             # stale -> will be refreshed
    serve(b"<html>captive portal</html>")
    assert net.cached("https://example.invalid/x.json", "x.json", validate=is_json_list) == p
    assert p.read_bytes() == b"[1]"
    assert net.cached("https://example.invalid/x.json", "x.json", refresh=True,
                      validate=is_json_list).read_bytes() == b"[1]"
    assert [f.name for f in cache.iterdir()] == ["x.json"]
    serve(b"[2]")
    assert net.cached("https://example.invalid/x.json", "x.json", validate=is_json_list).read_bytes() == b"[2]"


def test_size_caps(cache, tmp_path):
    serve(bytes(5000))
    assert len(net.get_bytes("https://example.invalid/a")) == 5000
    assert len(net.get_bytes("https://example.invalid/a", max_bytes=None)) == 5000
    with pytest.raises(net.NetError, match="more data"):
        net.get_bytes("https://example.invalid/a", max_bytes=4999)
    with pytest.raises(net.NetError):
        net.cached("https://example.invalid/a", "a.bin", max_bytes=100)
    assert not (cache / "a.bin").exists()
    with pytest.raises(net.NetError, match="larger"):
        net.download("https://example.invalid/a", tmp_path / "a.bin", max_bytes=100)
    assert not list(tmp_path.glob("a.bin*"))
    assert net.download("https://example.invalid/a", tmp_path / "a.bin", max_bytes=5000).stat().st_size == 5000


def test_errors_become_neterror(cache):
    serve(b"nope", 500)
    with pytest.raises(net.NetError):
        net.get_bytes("https://example.invalid/a")
    serve(b"", 404)
    assert net.get_bytes("https://example.invalid/a", ok404=True) is None
    serve(b"not json")
    with pytest.raises(net.NetError):
        net.get_json("https://example.invalid/a")
    serve(b'{"a": 1}')
    assert net.get_json("https://example.invalid/a") == {"a": 1}
