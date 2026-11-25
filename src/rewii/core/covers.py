"""Cover art from GameTDB (``art.gametdb.com``) and custom banners from RiiConnect24.

Covers are saved where USB Loader GX looks for them (``DriveLayout.gx_cover_dir``),
optionally where WiiFlow Lite looks (``wiiflow/covers`` for flat covers,
``wiiflow/boxcovers`` for full covers), and always into a local cache
(``cache_dir()/covers/<kind>/<ID>.png``) so the UI can show them offline.

Region selection mirrors USB Loader GX's ``ImageDownloader``: the 4th character of
the game ID picks the first region to try, then US, EN and the remaining regions.
"""
from __future__ import annotations

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from . import config, net
from .config import Settings
from .layout import DriveLayout
from .progress import CancelToken, Cancelled, ProgressFn, Reporter

ART_URL = "https://art.gametdb.com/wii/{kind}/{region}/{game_id}.png"
BANNER_URL = "https://banner.rc24.xyz/{id}.bnr"
"""Custom banners (WiiFlow Lite's default ``custom_banner_url``); mostly GameCube games.
Served by ID6 or ID3."""

COVER_KINDS = ("cover3D", "cover", "coverfull", "coverfullHQ", "disc")
# extra art types tried when the requested one is missing everywhere
KIND_FALLBACKS: dict[str, tuple[str, ...]] = {
    "coverfullHQ": ("coverfull",),
    "coverfull": ("coverfullHQ",),
    "disc": ("disccustom",),
}
PAL_LANG_BY_CHAR = {"D": "DE", "F": "FR", "S": "ES", "I": "IT", "H": "NL"}
PAL_CHARS = set("PDFISHUXYZLMRV")
OTHER_REGIONS = ("OTHER", "AU", "DE", "FR", "ES", "IT", "NL", "PT", "RU", "JA", "KO", "ZH")
# USB Loader GX rejects these sizes as GameTDB placeholder images
_PLACEHOLDER_SIZES = {36864, 7386, 4446}
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

OK = "ok"
MISSING = "missing"
ERROR = "error"
SKIPPED = "skipped"   # already on the drive


def cover_regions(game_id: str, lang: str = "EN") -> list[str]:
    """Ordered GameTDB region folders to try for ``game_id``."""
    c = game_id[3:4].upper()
    lang = (lang or "EN").upper()
    if c in ("E", "N"):
        first = ["US"]
    elif c == "J":
        first = ["JA"]
    elif c in ("K", "Q", "T"):
        first = ["KO"]
    elif c == "W":
        first = ["ZH"]
    elif c in PAL_CHARS:
        first = [PAL_LANG_BY_CHAR.get(c, lang), "EN"]
        if c == "U":
            first.insert(0, "AU")
        if c == "R":
            first.insert(1, "RU")
    else:
        first = [lang]
    return list(dict.fromkeys([*first, "US", "EN", *OTHER_REGIONS]))


def _valid_png(data: Optional[bytes]) -> bool:
    return (data is not None and len(data) > 1024 and data[:8] == _PNG_MAGIC
            and len(data) not in _PLACEHOLDER_SIZES)


def cache_path(game_id: str, kind: str) -> Path:
    """Where the local cached copy of a cover lives (may not exist)."""
    return config.cache_dir() / "covers" / kind / f"{game_id.upper()}.png"


def cached_cover_path(game_id: str, kind: str = "cover3D") -> Optional[Path]:
    """Path to the locally cached cover, or None if it hasn't been downloaded."""
    p = cache_path(game_id, kind)
    return p if p.is_file() else None


def wiiflow_cover_path(layout: DriveLayout, game_id: str, kind: str) -> Optional[Path]:
    """WiiFlow Lite location for ``kind`` (flat -> covers/, full -> boxcovers/), or None."""
    if kind == "cover":
        return layout.wiiflow / "covers" / f"{game_id}.png"
    if kind in ("coverfull", "coverfullHQ"):
        return layout.wiiflow / "boxcovers" / f"{game_id}.png"
    return None


def gx_cover_path(layout: DriveLayout, game_id: str, kind: str) -> Path:
    """USB Loader GX location for a cover of ``kind``."""
    return layout.gx_cover_dir(kind) / f"{game_id}.png"


def download_cover(game_id: str, kind: str, lang: str = "EN",
                   cancel: Optional[CancelToken] = None) -> tuple[Optional[bytes], str]:
    """Try every region (and fallback art types) for one cover.

    Returns ``(png_bytes, region)``; ``(None, "")`` when GameTDB has none.
    Raises :class:`net.NetError` only if *every* attempt failed with a network error.
    """
    last_err: Optional[net.NetError] = None
    any_answer = False
    for k in (kind, *KIND_FALLBACKS.get(kind, ())):
        for region in cover_regions(game_id, lang):
            if cancel is not None:
                cancel.check()
            url = ART_URL.format(kind=k, region=region, game_id=game_id)
            try:
                data = net.get_bytes(url, ok404=True)
            except net.NetError as e:
                last_err = e
                continue
            any_answer = True
            if _valid_png(data):
                return data, region
    if last_err is not None and not any_answer:
        raise last_err
    return None, ""


@dataclass
class CoverReport:
    """Outcome of :func:`fetch_covers`: ``results[game_id][kind]`` is ok/missing/error/skipped."""

    results: dict[str, dict[str, str]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)   # "ID/kind" -> message

    def count(self, status: str) -> int:
        return sum(1 for kinds in self.results.values() for s in kinds.values() if s == status)

    @property
    def ok(self) -> int:
        return self.count(OK) + self.count(SKIPPED)

    @property
    def missing(self) -> int:
        return self.count(MISSING)

    def missing_ids(self) -> list[str]:
        return [g for g, kinds in self.results.items() if MISSING in kinds.values()]


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def _targets(layout: DriveLayout, game_id: str, kind: str, gx: bool, wiiflow: bool) -> list[Path]:
    out: list[Path] = []
    if gx:
        out.append(gx_cover_path(layout, game_id, kind))
    if wiiflow:
        wf = wiiflow_cover_path(layout, game_id, kind)
        if wf is not None:
            out.append(wf)
    return out


def fetch_covers(layout: DriveLayout, game_ids: Iterable[str],
                 kinds: Optional[Iterable[str]] = None, settings: Optional[Settings] = None,
                 progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None,
                 *, wiiflow: Optional[bool] = None, usbloadergx: bool = True,
                 refresh: bool = False, overwrite: bool = False,
                 workers: int = 4) -> CoverReport:
    """Download covers for many games onto the drive (and the local cache).

    ``kinds`` defaults to ``settings.cover_types``. ``wiiflow=None`` writes WiiFlow
    copies only if a ``wiiflow/`` folder already exists. A cached cover is reused
    unless ``refresh``; covers already on the drive are skipped unless ``overwrite``.
    Raises :class:`Cancelled` if cancelled.
    """
    settings = settings or Settings()
    kinds_l = [k for k in (kinds or settings.cover_types) if k in COVER_KINDS]
    ids = list(dict.fromkeys(g.strip().upper() for g in game_ids if g and g.strip()))
    if wiiflow is None:
        wiiflow = layout.wiiflow.is_dir()
    lang = settings.cover_lang or "EN"
    report = CoverReport({g: {} for g in ids})
    jobs = [(g, k) for g in ids for k in kinds_l]
    rep = Reporter(progress, cancel, total=len(jobs), message="Downloading covers")
    rep.set_total(len(jobs))
    write_lock = threading.Lock()
    tok = cancel

    def work(gid: str, kind: str) -> tuple[str, str]:
        targets = _targets(layout, gid, kind, usbloadergx, bool(wiiflow))
        if not overwrite and targets and all(t.is_file() for t in targets):
            return SKIPPED, ""
        cp = cache_path(gid, kind)
        data: Optional[bytes] = None
        if cp.is_file() and not refresh:
            data = cp.read_bytes()
        else:
            try:
                data, _region = download_cover(gid, kind, lang, tok)
            except net.NetError as e:
                if cp.is_file():
                    data = cp.read_bytes()
                else:
                    return ERROR, str(e)
            if data is None:
                return MISSING, ""
            with write_lock:
                _write(cp, data)
        with write_lock:
            for t in targets:
                if overwrite or not t.is_file():
                    _write(t, data)
        return OK, ""

    if not jobs:
        return report
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(work, g, k): (g, k) for g, k in jobs}
        try:
            for fut in as_completed(futs):
                g, k = futs[fut]
                try:
                    status, err = fut.result()
                except Cancelled:
                    raise
                except OSError as e:
                    status, err = ERROR, str(e)
                report.results[g][k] = status
                if err:
                    report.errors[f"{g}/{k}"] = err
                rep.advance(1, f"{g} {k}: {status}")
        except Cancelled:
            for f in futs:
                f.cancel()
            raise
    return report


def cover_status(layout: DriveLayout, game_id: str) -> dict[str, bool]:
    """Which USB Loader GX cover kinds exist on the drive for ``game_id``.

    Also reports ``wiiflow_cover``/``wiiflow_box`` and ``cached`` (any local cache copy).
    """
    gid = game_id.upper()
    out = {k: gx_cover_path(layout, gid, k).is_file() for k in COVER_KINDS}
    out["wiiflow_cover"] = (layout.wiiflow / "covers" / f"{gid}.png").is_file()
    out["wiiflow_box"] = (layout.wiiflow / "boxcovers" / f"{gid}.png").is_file()
    out["cached"] = any(cached_cover_path(gid, k) is not None for k in COVER_KINDS)
    return out


def best_cached_cover(game_id: str,
                      prefer: Iterable[str] = ("cover", "cover3D", "coverfullHQ",
                                               "coverfull", "disc")) -> Optional[Path]:
    """First cached cover found in ``prefer`` order (handy for previews)."""
    for k in prefer:
        p = cached_cover_path(game_id, k)
        if p is not None:
            return p
    return None


def remove_covers(layout: DriveLayout, game_id: str) -> int:
    """Delete every cover for ``game_id`` from the drive (not the cache). Returns count."""
    gid = game_id.upper()
    n = 0
    paths = {gx_cover_path(layout, gid, k) for k in COVER_KINDS}
    paths |= {layout.wiiflow / "covers" / f"{gid}.png", layout.wiiflow / "boxcovers" / f"{gid}.png"}
    for p in paths:
        if p.is_file():
            p.unlink()
            n += 1
    return n


# --------------------------------------------------------------------------- banners
def gx_banner_dir(layout: DriveLayout) -> Path:
    """USB Loader GX ``BNRCachePath`` (``apps/usbloader_gx/cache_bnr/``, per CSettings.cpp)."""
    return layout.usbloadergx / "cache_bnr"


def wiiflow_banner_dir(layout: DriveLayout) -> Path:
    """WiiFlow Lite ``dir_custom_banners`` (``wiiflow/custom_banners``)."""
    return layout.wiiflow / "custom_banners"


def _valid_banner(data: Optional[bytes]) -> bool:
    return (data is not None and len(data) > 0x5000 and data[:1] != b"<"
            and b"IMET" in data[:0x100])


def download_custom_banner(layout: DriveLayout, game_id: str, *,
                           wiiflow: Optional[bool] = None, refresh: bool = False,
                           overwrite: bool = False) -> Optional[Path]:
    """Fetch a custom ``.bnr`` from banner.rc24.xyz (ID6, then ID3) into USB Loader GX's
    ``cache_bnr/<ID6>.bnr`` (and WiiFlow's ``custom_banners/`` if used).

    Returns the GX path, or None if no banner exists. Mostly available for GameCube
    games. Raises :class:`net.NetError` when offline and nothing is cached.
    """
    gid = game_id.strip().upper()
    if wiiflow is None:
        wiiflow = layout.wiiflow.is_dir()
    dest = gx_banner_dir(layout) / f"{gid}.bnr"
    cp = config.cache_dir() / "banners" / f"{gid}.bnr"
    data: Optional[bytes] = None
    if dest.is_file() and not overwrite and not refresh:
        data = dest.read_bytes()
    elif cp.is_file() and not refresh:
        data = cp.read_bytes()
    else:
        for ident in dict.fromkeys((gid, gid[:3])):
            d = net.get_bytes(BANNER_URL.format(id=ident), ok404=True)
            if _valid_banner(d):
                data = d
                break
        if data is None:
            return None
        _write(cp, data)
    if overwrite or refresh or not dest.is_file():
        _write(dest, data)
    if wiiflow:
        wf = wiiflow_banner_dir(layout) / f"{gid}.bnr"
        if overwrite or not wf.is_file():
            wf.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(dest, wf)
    return dest
