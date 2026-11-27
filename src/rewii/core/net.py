"""Shared HTTP helpers: one client, a polite User-Agent, on-disk caching, downloads."""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Optional

import httpx

from .. import __version__
from .config import cache_dir
from .progress import CancelToken, ProgressFn, Reporter

USER_AGENT = f"ReWii/{__version__} (+https://github.com/rewii)"
TIMEOUT = httpx.Timeout(30.0, connect=15.0)


class NetError(Exception):
    pass


_client: Optional[httpx.Client] = None


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
                               follow_redirects=True)
    return _client


def set_client(c: Optional[httpx.Client]) -> None:
    """Tests inject an httpx.Client with a MockTransport here."""
    global _client
    _client = c


def get(url: str, **kw) -> httpx.Response:
    try:
        r = client().get(url, **kw)
    except httpx.HTTPError as e:
        raise NetError(f"Could not reach {httpx.URL(url).host}: {e}") from e
    return r


def get_bytes(url: str, ok404: bool = False) -> bytes | None:
    r = get(url)
    if r.status_code == 404 and ok404:
        return None
    if r.status_code != 200:
        raise NetError(f"{url} returned HTTP {r.status_code}")
    return r.content


def get_json(url: str):
    r = get(url)
    if r.status_code != 200:
        raise NetError(f"{url} returned HTTP {r.status_code}")
    return r.json()


def cached(url: str, name: str, max_age: float = 7 * 86400, refresh: bool = False) -> Path:
    """Download ``url`` into the cache as ``name`` unless a fresh copy exists.

    Falls back to a stale copy when offline.
    """
    p = cache_dir() / name
    p.parent.mkdir(parents=True, exist_ok=True)
    fresh = p.exists() and (time.time() - p.stat().st_mtime) < max_age
    if fresh and not refresh:
        return p
    try:
        data = get_bytes(url)
    except NetError:
        if p.exists():
            return p
        raise
    tmp = p.with_suffix(p.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(p)
    return p


def download(url: str, dest: Path, *, md5: str | None = None, progress: ProgressFn | None = None,
             cancel: CancelToken | None = None) -> Path:
    """Stream a download to ``dest`` with progress; verifies md5 if given."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    rep = Reporter(progress, cancel, message=f"Downloading {dest.name}")
    h = hashlib.md5()
    try:
        with client().stream("GET", url) as r:
            if r.status_code != 200:
                raise NetError(f"{url} returned HTTP {r.status_code}")
            rep.set_total(int(r.headers.get("content-length") or 0))
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(256 * 1024):
                    f.write(chunk)
                    h.update(chunk)
                    rep.advance(len(chunk))
    except httpx.HTTPError as e:
        tmp.unlink(missing_ok=True)
        raise NetError(f"Download failed: {e}") from e
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if md5 and h.hexdigest().lower() != md5.lower():
        tmp.unlink(missing_ok=True)
        raise NetError(f"{dest.name}: checksum mismatch")
    os.replace(tmp, dest)
    return dest
