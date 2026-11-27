"""Shared HTTP helpers: one client, a polite User-Agent, on-disk caching, downloads."""
from __future__ import annotations

import hashlib
import re
import json
import os
import time
from pathlib import Path
from typing import Optional
from collections.abc import Callable

import httpx

from .. import __version__
from . import config, fileops
from .progress import CancelToken, ProgressFn, Reporter
from .errors import ReWiiError

USER_AGENT = f"ReWii/{__version__} (+https://github.com/AndyAnderson8/rewii)"
TIMEOUT = httpx.Timeout(30.0, connect=15.0)


class NetError(ReWiiError):
    pass


_client: Optional[httpx.Client] = None
#: Transport for the default client (None = real network). The test suite sets a
#: transport here that refuses every request, so nothing un-mocked reaches the internet.
transport: Optional[httpx.BaseTransport] = None


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
                               follow_redirects=True, transport=transport)
    return _client


def set_client(c: Optional[httpx.Client]) -> None:
    """Tests inject an httpx.Client with a MockTransport here."""
    global _client
    _client = c


MAX_BYTES = 64 * 1024 ** 2
"""Default cap for in-memory downloads (:func:`get_bytes`, :func:`get_json`, :func:`cached`)."""


def _host(url: str) -> str:
    try:
        return httpx.URL(url).host or url
    except Exception:
        return url


def _unreachable(url: str, e: BaseException) -> NetError:
    """The error for "the request never got an answer" - nearly always no internet."""
    detail = re.sub(r"^\[(Errno|WinError) -?\d+\]\s*", "", str(e)) or type(e).__name__
    return NetError("Couldn't connect \u2014 check your internet connection "
                    f"({_host(url)}: {detail}).")


def get(url: str, **kw) -> httpx.Response:
    try:
        r = client().get(url, **kw)
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise _unreachable(url, e) from e
    return r


def get_bytes(url: str, ok404: bool = False, *, max_bytes: int | None = MAX_BYTES) -> bytes | None:
    """GET ``url`` into memory. ``None`` for a 404 when ``ok404``.

    Raises :class:`NetError` for other statuses, network problems, or a body
    larger than ``max_bytes`` (``None`` = unlimited).
    """
    buf = bytearray()
    try:
        with client().stream("GET", url) as r:
            if r.status_code == 404 and ok404:
                return None
            if r.status_code != 200:
                raise NetError(f"{url} returned HTTP {r.status_code}")
            for chunk in r.iter_bytes(256 * 1024):
                buf += chunk
                if max_bytes is not None and len(buf) > max_bytes:
                    raise NetError(f"{_host(url)} sent more data than expected")
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise _unreachable(url, e) from e
    return bytes(buf)


def get_json(url: str, *, max_bytes: int | None = MAX_BYTES):
    data = get_bytes(url, max_bytes=max_bytes)
    assert data is not None
    try:
        return json.loads(data)
    except ValueError as e:
        raise NetError(f"{_host(url)} sent invalid data") from e


def cached(url: str, name: str, max_age: float = 7 * 86400, refresh: bool = False, *,
           validate: Callable[[Path], bool] | None = None,
           max_bytes: int | None = MAX_BYTES) -> Path:
    """Download ``url`` into the cache as ``name`` unless a fresh copy exists.

    Falls back to a stale copy when offline. ``validate(path)`` is run on the
    downloaded file *before* it replaces the cached one: invalid data (a captive
    portal page, an error document) never overwrites a good copy and is never
    kept - the old copy is returned, or :class:`NetError` raised if there is none.
    """
    p = config.cache_dir() / name
    p.parent.mkdir(parents=True, exist_ok=True)
    fresh = p.exists() and (time.time() - p.stat().st_mtime) < max_age
    if fresh and not refresh:
        return p
    try:
        data = get_bytes(url, max_bytes=max_bytes)
        assert data is not None
    except NetError:
        if p.exists():
            return p
        raise
    tmp = fileops.part_path(p, unique=True)
    try:
        tmp.write_bytes(data)
        if validate is not None and not validate(tmp):
            tmp.unlink(missing_ok=True)
            if p.exists():
                return p
            raise NetError(f"{_host(url)} sent invalid data")
        tmp.replace(p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return p


class ChecksumMismatch(NetError):
    """A download finished but isn't the file that was expected (nothing was kept)."""


def download(url: str, dest: Path, *, md5: str | None = None, sha256: str | None = None,
             progress: ProgressFn | None = None,
             cancel: CancelToken | None = None, max_bytes: int | None = None) -> Path:
    """Stream a download to ``dest`` with progress.

    ``md5`` / ``sha256`` (hex) are checked before the file gets its real name:
    on a mismatch :class:`ChecksumMismatch` is raised and nothing is kept.
    ``max_bytes`` aborts (with :class:`NetError`) a download that is larger.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = fileops.part_path(dest)
    rep = Reporter(progress, cancel, message=f"Downloading {dest.name}")
    h = hashlib.md5()
    h256 = hashlib.sha256() if sha256 else None
    got = 0
    try:
        with client().stream("GET", url) as r:
            if r.status_code != 200:
                raise NetError(f"{url} returned HTTP {r.status_code}")
            try:
                length = int(r.headers.get("content-length") or 0)
            except ValueError:
                length = 0
            if max_bytes is not None and length > max_bytes:
                raise NetError(f"{dest.name}: the download is larger than expected")
            rep.set_total(length)
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(256 * 1024):
                    got += len(chunk)
                    if max_bytes is not None and got > max_bytes:
                        raise NetError(f"{dest.name}: the download is larger than expected")
                    f.write(chunk)
                    h.update(chunk)
                    if h256 is not None:
                        h256.update(chunk)
                    rep.advance(len(chunk))
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        tmp.unlink(missing_ok=True)
        raise _unreachable(url, e) from e
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if (md5 and h.hexdigest().lower() != md5.lower()) or \
            (h256 is not None and sha256 and h256.hexdigest().lower() != sha256.lower()):
        tmp.unlink(missing_ok=True)
        raise ChecksumMismatch(f"{dest.name}: checksum mismatch")
    os.replace(tmp, dest)
    return dest
