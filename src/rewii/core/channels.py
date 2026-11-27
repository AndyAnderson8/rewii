"""One entry point for making a Wii Menu channel for any game (Wii or GameCube)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .config import Settings
from .disc import disc_info
from .forwarder import ChannelInfo, ForwarderError, brick_risk, cover_for_channel, make_channel


def channel_problem(platform: str, disc_number: int, game_id: str, on_drive: bool) -> Optional[str]:
    """Why a channel can't be made for this game, or None."""
    risk = brick_risk(game_id)
    if risk:
        return f"{risk} bricks the Wii Menu as a channel"
    if platform == "gc":
        if disc_number:
            return "make the channel for disc 1 (Nintendont switches discs itself)"
        if not on_drive:
            return "put the game on the drive first (GameCube channels point at the drive copy)"
    return None


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
