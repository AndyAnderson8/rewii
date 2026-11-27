"""Channel building shared by the Games and Channels sections."""
from __future__ import annotations

from typing import Any, Optional
from collections.abc import Callable, Iterable, Sequence

from ...core import channels as CH
from ...core import games as G
from ...core import wad as W
from ...core.config import Settings
from ...core.fileops import explain_os_error
from ...core.layout import DriveLayout
from ...core.progress import Cancelled, ProgressEvent, Reporter, UNIT_STEPS


def collision_warning(selected: Sequence[G.Game], others: Iterable[G.Game] = ()) -> str:
    """A warning when two games would get the same channel ID (the second channel
    would replace the first on the Wii Menu), or "" when there is no clash.

    ``selected`` are the games a channel is being made for; ``others`` are games
    that may already have one (see ``core.channels.channel_id_collisions``).
    """
    names: dict[str, str] = {}
    platforms: dict[str, str] = {}
    for g in [*others, *selected]:
        names[g.game_id] = g.title
        platforms[g.game_id] = g.platform
    mine = {g.game_id for g in selected}
    try:
        clashes = CH.channel_id_collisions(list(platforms.items()))
    except Exception:  # noqa: BLE001 - a warning must never block the action
        return ""
    lines = []
    for cid, ids in sorted(clashes.items()):
        ids = list(dict.fromkeys(ids))
        if len(ids) > 1 and mine.intersection(ids):
            lines.append(f"  {cid}: " + ", ".join(f"{names.get(i, i)} [{i}]" for i in ids))
    if not lines:
        return ""
    return ("Heads up - these games would get the same channel ID, so on the Wii Menu one "
            "channel replaces the other:\n" + "\n".join(lines))


def build_channels(games: Sequence[G.Game], settings: Settings, layout: Optional[DriveLayout],
                   info: Callable[[str], Any], stage: bool,
                   progress: Any, cancel: Any) -> tuple[list[Any], list[str]]:
    """Build a channel WAD per game (and copy it to the drive's wad/ folder when
    ``stage``). Returns ``(channels, errors)``; one failing game doesn't stop the rest."""
    rep = Reporter(progress, cancel, total=len(games), unit=UNIT_STEPS)
    out: list[Any] = []
    errs: list[str] = []
    def step_notes(title: str) -> Callable[[ProgressEvent], None]:
        """Show build_channel's steps ("Getting the cover art"...) under the game's name."""
        return lambda ev: rep.note(f"{title}: {ev.message}")

    for g in games:
        rep.note(f"Building channel for {g.title}")
        try:
            ci = CH.build_channel(g.path, settings, layout=layout,
                                  title=g.title, info=info(g.game_id), progress=step_notes(g.title))
            if stage and layout is not None:
                W.stage_wads(layout, [ci.path], settings=settings)
            out.append(ci)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - reported per game
            errs.append(f"{g.title}: {explain_os_error(e, layout.root if layout else None)}")
        rep.advance(1)
    return out, errs
