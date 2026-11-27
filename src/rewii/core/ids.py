"""Validation of game / title identifiers that come from untrusted places
(disc headers, save files, file names) before they are used in URLs or paths."""
from __future__ import annotations

import re

_GAME_ID = re.compile(r"[A-Z0-9]{4}(?:[A-Z0-9]{2})?")
_ID4 = re.compile(r"[A-Za-z0-9]{4}")
_UNSAFE = re.compile(r"[^A-Za-z0-9]")

# ---- disc IDs: the one definition (disc headers, "Title [ID]" folders, channels) ----
#: A 6-character disc ID. Lower case is allowed: a few unlicensed discs use it.
DISC_ID_PATTERN = "[A-Za-z0-9]{6}"
DISC_ID_RE = re.compile(DISC_ID_PATTERN)
_DISC_ID_BYTES_RE = re.compile(DISC_ID_PATTERN.encode("ascii"))
#: ``[RMCE01]`` as in ReWii's / the loaders' folder names; group 1 is the ID.
BRACKETED_ID_RE = re.compile(r"\[(" + DISC_ID_PATTERN + r")\]")
BRACKETED_ID_BYTES_RE = re.compile(rb"\[(" + DISC_ID_PATTERN.encode("ascii") + rb")\]")
#: First letters of GameCube disc IDs (G: games, D: demos/special discs, P: promo discs).
GC_ID_PREFIXES = ("G", "D", "P")
PLATFORM_WII, PLATFORM_GC = "wii", "gc"


def is_disc_id(game_id: object) -> bool:
    """True for exactly six ASCII letters/digits (text or bytes), as in a disc header."""
    if isinstance(game_id, (bytes, bytearray)):
        return _DISC_ID_BYTES_RE.fullmatch(bytes(game_id)) is not None
    return isinstance(game_id, str) and DISC_ID_RE.fullmatch(game_id) is not None


def id_in_brackets(name: str) -> str | None:
    """The disc ID in a ``Title [GAMEID]`` style name, or None."""
    m = BRACKETED_ID_RE.search(name)
    return m.group(1) if m else None


def platform_guess(game_id: str) -> str:
    """``"gc"`` or ``"wii"`` from the first letter of a disc ID - only a guess, for when
    the disc itself can't be read (a file that still needs converting, a bare ID)."""
    return PLATFORM_GC if game_id[:1].upper() in GC_ID_PREFIXES else PLATFORM_WII


def valid_game_id(game_id: object) -> bool:
    """True for a 4-character (channel/WiiWare) or 6-character (disc) ID made only
    of ASCII letters and digits (case-insensitive)."""
    return isinstance(game_id, str) and _GAME_ID.fullmatch(game_id.strip().upper()) is not None


def valid_id4(game_id: object) -> bool:
    """True for exactly four ASCII letters/digits (the low word of a title ID)."""
    return isinstance(game_id, str) and _ID4.fullmatch(game_id) is not None


def id_for_filename(game_id: str) -> str:
    """``game_id`` upper-cased with every character that isn't a letter or digit
    replaced by ``_``, so it can never change the folder a file name lands in."""
    return _UNSAFE.sub("_", (game_id or "").strip().upper()) or "_"
