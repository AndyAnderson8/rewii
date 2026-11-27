"""Shared test setup: every test is hermetic.

* settings, cache and the default library live under a per-test temp folder
  (``REWII_CONFIG_DIR`` / ``REWII_CACHE_DIR`` / ``HOME``), never the real user folders;
* :mod:`rewii.core.net` gets a transport that refuses every request - a test that needs
  HTTP installs its own ``httpx.MockTransport`` with ``net.set_client``;
* ``requests`` (used inside libWiiPy) and DNS lookups for anything but this machine fail.
"""
import pathlib
import socket
import sys

import httpx
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from rewii.core import net  # noqa: E402

_LOCAL = {None, "", "localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"network is blocked in tests: {request.url}", request=request)


@pytest.fixture(autouse=True)
def _hermetic(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("rewii-user")
    monkeypatch.setenv("REWII_CONFIG_DIR", str(base / "config"))
    monkeypatch.setenv("REWII_CACHE_DIR", str(base / "cache"))
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(base / "home"))
    (base / "home").mkdir()

    monkeypatch.setattr(net, "transport", httpx.MockTransport(_refuse))
    net.set_client(None)

    import requests.adapters

    def no_requests(self, request, *a, **k):
        raise requests.exceptions.ConnectionError(f"network is blocked in tests: {request.url}")
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", no_requests)

    real_getaddrinfo = socket.getaddrinfo

    def local_only(host, *a, **k):
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        if host not in _LOCAL:
            raise socket.gaierror(socket.EAI_NONAME, f"DNS is blocked in tests: {host}")
        return real_getaddrinfo(host, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", local_only)
    yield
    net.set_client(None)


@pytest.fixture
def local_patch():
    """A MonkeyPatch of the test's own: ``local_patch.undo()`` mid-test only undoes what
    the test itself patched (``monkeypatch.undo()`` would also drop the isolation above)."""
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(autouse=True)
def _disk_guard(tmp_path, request):
    """Stop the run with a clear message if one test eats the disk (a non-sparse image
    on a small CI disk), instead of letting the machine die without a log."""
    import shutil as _sh
    before = _sh.disk_usage(tmp_path).free
    yield
    after = _sh.disk_usage(tmp_path).free
    used = before - after
    if used > 3 * 1024 ** 3 or after < 2 * 1024 ** 3:
        pytest.exit(f"{request.node.nodeid} used {used / 1e9:.1f} GB of temp space "
                    f"({after / 1e9:.1f} GB left) - a test file is not sparse here", returncode=3)
