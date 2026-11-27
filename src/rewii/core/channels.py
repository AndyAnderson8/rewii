"""One entry point for making a Wii Menu channel for any game (Wii or GameCube)."""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Optional
from collections.abc import Iterable

from .config import Settings
from .disc import disc_info
from .fileops import write_atomic
from .forwarder import (BannerBrickRisk, ChannelInfo, ForwarderError, brick_risk, channel_id_for,
                        make_channel)
from .ids import platform_guess
from .layout import DriveLayout
from .progress import ProgressFn, Reporter


def channel_problem(platform: str, disc_number: int, game_id: str, on_drive: bool) -> Optional[str]:
    """Why a channel can't be made for this game, or None.

    The banner-brick list only concerns Wii games (their channel shows the
    game's own banner; GameCube channels use ReWii's template)."""
    risk = brick_risk(game_id) if platform != "gc" else None
    if risk:
        return f"{risk} bricks the Wii Menu as a channel"
    if platform == "gc":
        if disc_number:
            return "make the channel for disc 1 (Nintendont switches discs itself)"
        if not on_drive:
            return "put the game on the drive first (GameCube channels point at the drive copy)"
    return None


def channel_id_of(game_id: str, platform: str = "wii") -> str:
    """The Wii Menu title ID a game's channel gets ('U'/'G' + ID characters 2-4)."""
    if platform == "gc":
        from .gcforwarder import gc_channel_id
        return gc_channel_id(game_id)
    return channel_id_for(game_id)


def channel_id_collisions(game_ids: Iterable[str | tuple[str, str]]) -> dict[str, list[str]]:
    """Channel IDs that more than one of these games would get.

    The channel ID keeps only characters 2-4 of the game ID, so two games that
    differ in the first letter (or only in the 2-character maker code) collide:
    installing the second channel replaces the first on the Wii Menu. Returns
    ``{channel_id: [game_id, ...]}`` for the clashing ones only.

    Items are game IDs, or ``(game_id, platform)`` pairs with platform "wii" or
    "gc". For a bare ID the platform is guessed from its first letter (G, D and
    P are GameCube); pass pairs when the platform is known."""
    groups: dict[str, list[str]] = {}
    for item in game_ids:
        gid, platform = item if isinstance(item, tuple) else (item, platform_guess(item))
        if len(gid) < 4:
            continue
        ids = groups.setdefault(channel_id_of(gid, platform), [])
        if gid not in ids:
            ids.append(gid)
    return {chan: ids for chan, ids in groups.items() if len(ids) > 1}


def cover_for_channel(game_id: str, lang: str = "EN") -> bytes | None:
    """Front cover art for the "cover" launch style: the cache first, then GameTDB
    (network). None when there is no cover or it can't be fetched."""
    from . import covers
    for kind in ("cover", "coverfullHQ", "cover3D"):
        p = covers.cached_cover_path(game_id, kind)
        if p is not None:
            return p.read_bytes()
    try:
        data, _region = covers.download_cover(game_id, "cover", lang)
    except Exception:  # noqa: BLE001 - a channel without cover art is still a channel
        return None
    if data:
        with contextlib.suppress(OSError):
            write_atomic(covers.cache_path(game_id, "cover"), data)
    return data


def fetch_cert_chain() -> bytes:
    """Nintendo's certificate chain for a new channel (cached; needs the internet once)."""
    from . import nus
    try:
        return nus.cert_chain()
    except nus.NusError as e:
        raise ForwarderError(f"Need internet once to download the Wii certificate chain ({e})") from e


def build_channel(game_path: Path, settings: Settings, *, layout: DriveLayout | Path | None = None,
                  title: str | None = None, info: Any = None,
                  nintendont: bytes | None = None, cert_chain: bytes | None = None,
                  fat: bool = True, progress: ProgressFn | None = None) -> ChannelInfo:
    """Build a channel WAD into ``settings.channels_dir``.

    This is where everything a channel needs from outside is fetched, as explicit
    steps (each announced through ``progress``); the builders in ``forwarder`` and
    ``gcforwarder`` only work on the bytes they are given:

    1. Nintendo's certificate chain (cached; from NUS the first time),
    2. Wii games with the "cover" style: the cover art (cache, else GameTDB),
    3. GameCube games: Nintendont's boot.dol (Open Shop Channel, else the drive's copy).

    Pass ``cert_chain`` / ``nintendont`` to skip a fetch (e.g. when building many).
    ``info`` is an optional GameTDB entry (publisher, year, synopsis) used for
    GameCube banners. GameCube games must already be on the drive at ``game_path``.
    ``fat=False`` makes a Wii channel for an old WBFS-formatted drive.
    """
    rep = Reporter(progress)
    di = disc_info(game_path)
    if di.platform == "wii":
        risk = brick_risk(di.game_id)
        if risk:                       # refuse before fetching anything
            raise BannerBrickRisk(f"{risk} is known to brick the Wii Menu as a channel - refusing")
    elif layout is None:
        raise ForwarderError("GameCube channels need the game on the Wii drive")
    if cert_chain is None:
        rep.note("Getting the Wii certificate chain")
        cert_chain = fetch_cert_chain()
    if di.platform == "wii":
        cov = None
        if settings.forwarder_style == "cover":
            rep.note("Getting the cover art")
            cov = cover_for_channel(di.game_id, settings.cover_lang)
        rep.note("Building the channel")
        return make_channel(game_path, settings.channels_dir, style=settings.forwarder_style,
                            cover_png=cov, title=title, fat=fat, cert_chain=cert_chain)
    assert layout is not None
    from .gcforwarder import get_nintendont_dol, make_gc_channel
    if nintendont is None:
        rep.note("Getting Nintendont")
        nintendont = get_nintendont_dol(layout)
    publisher = getattr(info, "publisher", "") or ""
    year = str(getattr(info, "year", "") or "")
    synopsis = getattr(info, "synopsis", "") or ""
    rep.note("Building the channel")
    return make_gc_channel(game_path, layout, settings.channels_dir, title=title, publisher=publisher,
                           year=year, synopsis=synopsis, nintendont=nintendont, cert_chain=cert_chain)
