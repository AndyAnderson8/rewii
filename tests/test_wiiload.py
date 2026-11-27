"""Tests for rewii.core.wiiload against a local socket server."""
from __future__ import annotations

import io
import socket
import struct
import threading
import zipfile
import zlib

import pytest

from rewii.core import wiiload
from rewii.core.progress import CancelToken, Cancelled


class FakeHBC:
    """Accepts one wiiload connection and records what arrived."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.data = b""
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        conn, _ = self.sock.accept()
        with conn:
            chunks = []
            while True:
                b = conn.recv(65536)
                if not b:
                    break
                chunks.append(b)
        self.data = b"".join(chunks)
        self.sock.close()

    def parsed(self):
        self.t.join(5)
        d = self.data
        assert d[:4] == b"HAXX"
        major, minor, args_len, size, raw = struct.unpack(">BBHII", d[4:16])
        payload = d[16:16 + size]
        args = d[16 + size:]
        assert len(args) == args_len
        return (major, minor), payload, raw, args


def test_send_dol_compressed(tmp_path):
    f = tmp_path / "boot.dol"
    body = b"\x00\x01" * 50_000
    f.write_bytes(body)
    srv = FakeHBC()
    events = []
    wiiload.send("127.0.0.1", f, ["arg1", "x y"], events.append, port=srv.port)
    ver, payload, raw, args = srv.parsed()
    assert ver == (0, 5)
    assert raw == len(body) and zlib.decompress(payload) == body
    assert args == b"boot.dol\0arg1\0x y\0\0"
    assert events[-1].done == len(payload)


def test_send_uncompressible_and_no_args(tmp_path):
    import os
    f = tmp_path / "app.elf"
    body = os.urandom(5000)
    f.write_bytes(body)
    srv = FakeHBC()
    wiiload.send("127.0.0.1", f, port=srv.port)
    _, payload, raw, args = srv.parsed()
    assert raw == 0 and payload == body
    assert args == b"app.elf\0\0"


def test_send_zip_restructured(tmp_path):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("apps/myapp/boot.dol", b"DOL")
        z.writestr("apps/myapp/meta.xml", b"<app/>")
        z.writestr("controllers/x.ini", b"ini")
    f = tmp_path / "myapp.zip"
    f.write_bytes(b.getvalue())
    srv = FakeHBC()
    wiiload.send("127.0.0.1", f, port=srv.port)
    _, payload, raw, _ = srv.parsed()
    assert raw == 0
    names = zipfile.ZipFile(io.BytesIO(payload)).namelist()
    assert sorted(names) == ["myapp/boot.dol", "myapp/meta.xml"]


def test_hbc_zip_passthrough_and_errors():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("myapp/boot.elf", b"ELF")
    assert wiiload.hbc_zip(b.getvalue()) == b.getvalue()
    b2 = io.BytesIO()
    with zipfile.ZipFile(b2, "w") as z:
        z.writestr("readme.txt", b"x")
    with pytest.raises(wiiload.WiiloadError):
        wiiload.hbc_zip(b2.getvalue())


def test_header_and_args():
    h = wiiload.build_header(10, 20, 3)
    assert h == b"HAXX\x00\x05\x00\x03\x00\x00\x00\x0a\x00\x00\x00\x14"
    with pytest.raises(wiiload.WiiloadError):
        wiiload.build_args("a", ["x" * 1100])


def test_connection_refused(tmp_path):
    f = tmp_path / "boot.dol"
    f.write_bytes(b"x" * 10)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(wiiload.WiiloadError):
        wiiload.send("127.0.0.1", f, port=port, timeout=2)


def test_cancel(tmp_path):
    f = tmp_path / "boot.dol"
    f.write_bytes(b"x" * 10)
    tok = CancelToken()
    tok.cancel()
    with pytest.raises(Cancelled):
        wiiload.send("127.0.0.1", f, cancel=tok, port=1)
