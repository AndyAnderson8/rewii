"""Validation of game / title identifiers that come from untrusted places
(disc headers, save files, file names) before they are used in URLs or paths."""
from __future__ import annotations

import re

_GAME_ID = re.compile(r"[A-Z0-9]{4}(?:[A-Z0-9]{2})?")
_ID4 = re.compile(r"[A-Za-z0-9]{4}")
_UNSAFE = re.compile(r"[^A-Za-z0-9]")


class InvalidGameId(ValueError):
    """The text is not a 4- or 6-character game ID."""


def valid_game_id(game_id: object) -> bool:
    """True for a 4-character (channel/WiiWare) or 6-character (disc) ID made only
    of ASCII letters and digits (case-insensitive)."""
    return isinstance(game_id, str) and _GAME_ID.fullmatch(game_id.strip().upper()) is not None


def valid_id4(game_id: object) -> bool:
    """True for exactly four ASCII letters/digits (the low word of a title ID)."""
    return isinstance(game_id, str) and _ID4.fullmatch(game_id) is not None


def clean_game_id(game_id: str) -> str:
    """Upper-cased, stripped ID; raises :class:`InvalidGameId` if it isn't valid."""
    gid = (game_id or "").strip().upper() if isinstance(game_id, str) else ""
    if not _GAME_ID.fullmatch(gid):
        raise InvalidGameId(f"not a valid game ID: {game_id!r}")
    return gid


def id_for_filename(game_id: str) -> str:
    """``game_id`` upper-cased with every character that isn't a letter or digit
    replaced by ``_``, so it can never change the folder a file name lands in."""
    return _UNSAFE.sub("_", (game_id or "").strip().upper()) or "_"
