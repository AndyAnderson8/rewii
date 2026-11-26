"""One entry point for making a Wii Menu channel for any game (Wii or GameCube)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from collections.abc import Iterable

from .config import Settings
from .disc import disc_info
from .forwarder import (ChannelInfo, ForwarderError, brick_risk, channel_id_for, cover_for_channel,
                        make_channel)


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
        gid, platform = item if isinstance(item, tuple) else (item, "gc" if item[:1] in "GDP" else "wii")
        if len(gid) < 4:
            continue
        ids = groups.setdefault(channel_id_of(gid, platform), [])
        if gid not in ids:
            ids.append(gid)
    return {chan: ids for chan, ids in groups.items() if len(ids) > 1}


def build_channel(game_path: Path, settings: Settings, *, drive_root: Path | None = None,
                  title: str | None = None, info: Any = None,
                  nintendont: bytes | None = None) -> ChannelInfo:
    """Build a channel WAD into ``settings.channels_dir``.

    ``info`` is an optional GameTDB entry (publisher, year, synopsis) used for
    GameCube banners. GameCube games must already be on the drive at ``game_path``.
    """
    di = disc_info(game_path)
    if di.platform == "wii":
        cov = cover_for_channel(di.game_id, settings.cover_lang) if settings.forwarder_style == "cover" else None
        return make_channel(game_path, settings.channels_dir, style=settings.forwarder_style,
                            cover_png=cov, title=title)
    if drive_root is None:
        raise ForwarderError("GameCube channels need the game on the Wii drive")
    from .gcforwarder import make_gc_channel
    publisher = getattr(info, "publisher", "") or ""
    year = str(getattr(info, "year", "") or "")
    synopsis = getattr(info, "synopsis", "") or ""
    return make_gc_channel(game_path, drive_root, settings.channels_dir, title=title, publisher=publisher,
                           year=year, synopsis=synopsis, nintendont=nintendont)
