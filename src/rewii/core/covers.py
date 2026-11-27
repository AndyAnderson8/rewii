"""Cover art from GameTDB (``art.gametdb.com``) and custom banners from RiiConnect24.

Covers are saved where USB Loader GX looks for them (``DriveLayout.gx_cover_dir``),
optionally where WiiFlow Lite looks (``wiiflow/covers`` for flat covers,
``wiiflow/boxcovers`` for full covers), and always into a local cache
(``cache_dir()/covers/<kind>/<ID>.png``) so the UI can show them offline.

Region selection mirrors USB Loader GX's ``ImageDownloader``: the 4th character of
the game ID picks the first region to try, then US, EN and the remaining regions.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from collections.abc import Iterable

from . import config, net
from .config import Settings
from .errors import ReWiiError
from .fileops import explain_os_error, write_atomic
from .ids import id_for_filename, valid_game_id
from .layout import DriveLayout
from .progress import UNIT_STEPS, Cancelled, CancelToken, ProgressFn, Reporter

ART_URL = "https://art.gametdb.com/wii/{kind}/{region}/{game_id}.png"

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
ALL_KINDS = frozenset(COVER_KINDS) | {k for v in KIND_FALLBACKS.values() for k in v}

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


def _png_file_ok(path: Path) -> bool:
    """True if ``path`` is a usable cover (a real PNG, not empty/truncated/placeholder)."""
    try:
        if not path.is_file():
            return False
        size = path.stat().st_size
        if size <= 1024 or size in _PLACEHOLDER_SIZES:
            return False
        with open(path, "rb") as f:
            return f.read(8) == _PNG_MAGIC
    except OSError:
        return False


def cache_path(game_id: str, kind: str) -> Path:
    """Where the local cached copy of a cover lives (may not exist)."""
    if kind not in ALL_KINDS:
        raise CoverError(f"unknown cover kind {kind!r}")
    return config.cache_dir() / "covers" / kind / f"{id_for_filename(game_id)}.png"


def cached_cover_path(game_id: str, kind: str = "cover3D") -> Optional[Path]:
    """Path to the locally cached cover, or None if it hasn't been downloaded."""
    p = cache_path(game_id, kind)
    return p if p.is_file() else None


def wiiflow_cover_path(layout: DriveLayout, game_id: str, kind: str) -> Optional[Path]:
    """WiiFlow Lite location for ``kind`` (flat -> covers/, full -> boxcovers/), or None."""
    if kind == "cover":
        return layout.wiiflow_covers / f"{id_for_filename(game_id)}.png"
    if kind in ("coverfull", "coverfullHQ"):
        return layout.wiiflow_boxcovers / f"{id_for_filename(game_id)}.png"
    return None


def gx_cover_path(layout: DriveLayout, game_id: str, kind: str) -> Path:
    """USB Loader GX location for a cover of ``kind``."""
    return layout.gx_cover_dir(kind) / f"{id_for_filename(game_id)}.png"


def download_cover(game_id: str, kind: str, lang: str = "EN",
                   cancel: Optional[CancelToken] = None) -> tuple[Optional[bytes], str]:
    """Try every region (and fallback art types) for one cover.

    Returns ``(png_bytes, region)``; ``(None, "")`` when GameTDB has none (every
    region answered "not found") or ``game_id`` isn't a valid ID.
    Raises :class:`net.NetError` if no cover was found and at least one attempt
    failed for another reason (offline, HTTP 429/5xx ...): that cover may well
    exist, so it must not be reported as missing.
    """
    if not valid_game_id(game_id) or kind not in ALL_KINDS:
        return None, ""
    game_id = game_id.strip().upper()
    last_err: Optional[net.NetError] = None
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
            if _valid_png(data):
                return data, region
    if last_err is not None:
        raise last_err
    return None, ""


class CoverError(ReWiiError, ValueError):
    """A cover request that can't be carried out (unknown cover kind...)."""


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


def _write(path: Path, data: bytes) -> None:
    write_atomic(path, data)


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
                 kinds: Optional[Iterable[str]], settings: Settings,
                 progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None,
                 *, wiiflow: Optional[bool] = None, usbloadergx: bool = True,
                 refresh: bool = False, overwrite: bool = False,
                 workers: int = 4) -> CoverReport:
    """Download covers for many games onto the drive (and the local cache).

    ``kinds=None`` means ``settings.cover_types`` (``settings`` is required: the cover
    types and language are the user's choice, never silent defaults). ``wiiflow=None`` writes WiiFlow
    copies only if a ``wiiflow/`` folder already exists. A cached cover is reused
    unless ``refresh``; covers already on the drive are skipped unless ``overwrite``.
    Raises :class:`Cancelled` if cancelled.
    """
    layout.require_root()
    kinds_l = [k for k in (kinds or settings.cover_types) if k in COVER_KINDS]
    ids = list(dict.fromkeys(g.strip().upper() for g in game_ids if g and g.strip()))
    if wiiflow is None:
        wiiflow = layout.has_wiiflow
    lang = settings.cover_lang or "EN"
    report = CoverReport({g: {} for g in ids})
    jobs = [(g, k) for g in ids for k in kinds_l]
    rep = Reporter(progress, cancel, total=len(jobs), message="Downloading covers", unit=UNIT_STEPS)
    rep.set_total(len(jobs))
    write_lock = threading.Lock()
    tok = cancel

    def work(gid: str, kind: str) -> tuple[str, str]:
        if not valid_game_id(gid):
            return ERROR, f"{gid!r} is not a valid game ID"
        targets = _targets(layout, gid, kind, usbloadergx, bool(wiiflow))
        # a broken file on the drive (empty, truncated, not a PNG) doesn't count as "there"
        if not overwrite and targets and all(_png_file_ok(t) for t in targets):
            return SKIPPED, ""
        cp = cache_path(gid, kind)
        data: Optional[bytes] = None
        if not refresh and _png_file_ok(cp):
            data = cp.read_bytes()
        else:
            try:
                data, _region = download_cover(gid, kind, lang, tok)
            except net.NetError as e:
                if _png_file_ok(cp):
                    data = cp.read_bytes()
                else:
                    return ERROR, str(e)
            if data is None:
                return MISSING, ""
            with write_lock:
                _write(cp, data)
        with write_lock:
            for t in targets:
                if overwrite or not _png_file_ok(t):
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
                    status, err = ERROR, explain_os_error(e, layout.root)
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
    gid = id_for_filename(game_id)
    out = {k: gx_cover_path(layout, gid, k).is_file() for k in COVER_KINDS}
    out["wiiflow_cover"] = (layout.wiiflow_covers / f"{gid}.png").is_file()
    out["wiiflow_box"] = (layout.wiiflow_boxcovers / f"{gid}.png").is_file()
    out["cached"] = any(cached_cover_path(gid, k) is not None for k in COVER_KINDS)
    return out


# --------------------------------------------------------------------------- banners

