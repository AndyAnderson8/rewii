"""Gecko cheat codes: fetch ``txtcodes`` files, parse them, build ``.gct`` files.

Sources: ``codes.rc24.xyz`` (the GeckoCodes.org successor used by WiiFlow Lite's
default ``cheats_url``). There is no maintained per-game GitHub mirror (the old
``wiidev/cheats`` repo does not exist), so extra sources can be appended to
:data:`CHEAT_SOURCES`.

On the drive:

* USB Loader GX: ``txtcodes/<ID>.txt`` and ``codes/<ID>.gct`` (CSettings.cpp)
* WiiFlow Lite: ``wiiflow/codes/<ID>.txt`` and ``wiiflow/codes/gct/<ID>.gct``

txtcodes format (as parsed by USB Loader GX's ``GCTCheats::openTxtfile``)::

    RMCE01                      <- game ID
    Mario Kart Wii              <- title
                                <- blank line
    Code name [Author]          <- cheat name
    C26FE200 00000005           <- code lines, 8+8 hex digits (letters = placeholders)
    Optional comment lines
                                <- blank line ends the cheat
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from . import config, net
from .layout import DriveLayout

CHEAT_SOURCES: list[str] = [
    "https://codes.rc24.xyz/txt.php?txt={game_id}",
]

GCT_HEADER = bytes.fromhex("00D0C0DE00D0C0DE")
GCT_FOOTER = bytes.fromhex("F000000000000000")

_CODE_RE = re.compile(r"^([0-9A-Za-z]{8}) ([0-9A-Za-z]{8})(?:\s.*)?$")
_HEX = set("0123456789ABCDEFabcdef")
_AUTHOR_RE = re.compile(r"\s*\[([^\]]+)\]\s*$")


class CheatError(Exception):
    """Bad txtcodes data or unfilled placeholders."""


@dataclass
class Cheat:
    """One cheat: a name, its code lines (``("C26FE200", "00000005")``) and comments."""

    name: str
    codes: list[tuple[str, str]] = field(default_factory=list)
    comment: str = ""

    @property
    def author(self) -> str:
        m = _AUTHOR_RE.search(self.name)
        return m.group(1) if m else ""

    @property
    def title(self) -> str:
        """Name without the trailing ``[Author]``."""
        return _AUTHOR_RE.sub("", self.name).strip() or self.name

    @property
    def placeholders(self) -> dict[str, int]:
        """Placeholder letters -> digit count of their longest run (e.g. ``{"X": 4}``)."""
        out: dict[str, int] = {}
        for a, b in self.codes:
            for m in re.finditer(r"([^0-9A-Fa-f])\1*", a + " " + b):
                ch = m.group(1)
                if ch == " ":
                    continue
                out[ch] = max(out.get(ch, 0), len(m.group(0)))
        return out

    @property
    def needs_values(self) -> bool:
        return bool(self.placeholders)

    def filled(self, values: Optional[Mapping[str, str | int]] = None) -> list[tuple[str, str]]:
        """Code lines with placeholders replaced.

        Each placeholder run (e.g. ``XXXX``) is replaced by the value for that letter,
        right-aligned and zero-padded to the run length. Ints are formatted as hex.
        """
        values = dict(values or {})
        out: list[tuple[str, str]] = []

        def sub(m: re.Match[str]) -> str:
            ch, n = m.group(1), len(m.group(0))
            if ch not in values:
                raise CheatError(f"'{self.title}': no value for placeholder {ch}")
            v = values[ch]
            s = format(v, "X") if isinstance(v, int) else str(v).strip()
            if s.lower().startswith("0x"):
                s = s[2:]
            if not s or any(c not in _HEX for c in s) or len(s) > n:
                raise CheatError(f"'{self.title}': {ch}={values[ch]!r} is not a {n}-digit hex value")
            return s.upper().rjust(n, "0")

        for a, b in self.codes:
            line = re.sub(r"([^0-9A-Fa-f ])\1*", sub, f"{a} {b}")
            x, y = line.split(" ")
            out.append((x.upper(), y.upper()))
        return out

    def to_bytes(self, values: Optional[Mapping[str, str | int]] = None) -> bytes:
        """Big-endian code bytes as they appear inside a ``.gct``."""
        return b"".join(struct.pack(">II", int(a, 16), int(b, 16))
                        for a, b in self.filled(values))


@dataclass
class CheatFile:
    """A parsed txtcodes file."""

    game_id: str
    title: str
    cheats: list[Cheat] = field(default_factory=list)

    def by_name(self, name: str) -> Optional[Cheat]:
        return next((c for c in self.cheats if c.name == name), None)


def parse_txtcodes(text: str | bytes) -> CheatFile:
    """Parse a txtcodes file. Cheats without code lines (headers/notes) are dropped,
    like USB Loader GX does."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    lines = [ln.rstrip() for ln in text.lstrip("﻿").splitlines()]
    if len(lines) < 1 or not lines[0].strip():
        raise CheatError("empty txtcodes file")
    game_id = lines[0].strip()
    title = lines[1].strip() if len(lines) > 1 else ""
    cheats: list[Cheat] = []
    cur: Optional[Cheat] = None
    comments: list[str] = []

    def finish() -> None:
        if cur is not None and cur.codes:
            cur.comment = "\n".join(comments).strip()
            cheats.append(cur)

    for ln in lines[2:]:
        if not ln.strip():
            finish()
            cur, comments = None, []
            continue
        if cur is None:
            cur = Cheat(ln.strip())
            continue
        m = _CODE_RE.match(ln.strip())
        if m and not comments:
            cur.codes.append((m.group(1), m.group(2)))
        else:
            comments.append(ln.strip())
    finish()
    return CheatFile(game_id, title, cheats)


def format_txtcodes(cf: CheatFile) -> str:
    """Serialize back to txtcodes text (CRLF, like the source files)."""
    out = [cf.game_id, cf.title, ""]
    for c in cf.cheats:
        out.append(c.name)
        out += [f"{a} {b}" for a, b in c.codes]
        if c.comment:
            out += c.comment.splitlines()
        out.append("")
    return "\r\n".join(out)


# --------------------------------------------------------------------------- drive paths
def txt_path(layout: DriveLayout, game_id: str) -> Path:
    return layout.txtcodes / f"{game_id.upper()}.txt"


def gct_path(layout: DriveLayout, game_id: str) -> Path:
    return layout.codes / f"{game_id.upper()}.gct"


def wiiflow_txt_path(layout: DriveLayout, game_id: str) -> Path:
    return layout.wiiflow / "codes" / f"{game_id.upper()}.txt"


def wiiflow_gct_path(layout: DriveLayout, game_id: str) -> Path:
    return layout.wiiflow / "codes" / "gct" / f"{game_id.upper()}.gct"


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def _looks_valid(data: Optional[bytes], game_id: str) -> bool:
    if not data:
        return False
    first = data.lstrip(b"\xef\xbb\xbf").split(b"\n", 1)[0].strip().decode("ascii", "replace")
    return first.upper() == game_id.upper()


def download_txtcodes(game_id: str) -> Optional[bytes]:
    """Raw txtcodes from the first source that has them; None if no source does.

    Raises :class:`net.NetError` only when every source was unreachable.
    """
    gid = game_id.strip().upper()
    last: Optional[net.NetError] = None
    reached = False
    for src in CHEAT_SOURCES:
        try:
            data = net.get_bytes(src.format(game_id=gid), ok404=True)
        except net.NetError as e:
            last = e
            continue
        reached = True
        if _looks_valid(data, gid):
            return data
    if last is not None and not reached:
        raise last
    return None


def fetch_cheats(layout: DriveLayout, game_id: str, *, wiiflow: Optional[bool] = None,
                 refresh: bool = False) -> Optional[CheatFile]:
    """Make sure ``txtcodes/<ID>.txt`` exists on the drive and return it parsed.

    Uses the drive copy (then the local cache) unless ``refresh``. Returns None if no
    cheats exist for this game. ``wiiflow=None`` also writes WiiFlow's copy if a
    ``wiiflow/`` folder exists. Raises :class:`net.NetError` if offline with no copy.
    """
    gid = game_id.strip().upper()
    if wiiflow is None:
        wiiflow = layout.wiiflow.is_dir()
    dest = txt_path(layout, gid)
    cache = config.cache_dir() / "cheats" / f"{gid}.txt"
    data: Optional[bytes] = None
    if not refresh:
        for p in (dest, cache):
            if p.is_file():
                data = p.read_bytes()
                break
    if data is None:
        try:
            data = download_txtcodes(gid)
        except net.NetError:
            if cache.is_file():
                data = cache.read_bytes()
            else:
                raise
        if data is None:
            return None
        _write(cache, data)
    if refresh or not dest.is_file():
        _write(dest, data)
    if wiiflow:
        wf = wiiflow_txt_path(layout, gid)
        if refresh or not wf.is_file():
            _write(wf, data)
    return parse_txtcodes(data)


def load_cheats(layout: DriveLayout, game_id: str) -> Optional[CheatFile]:
    """Parse the drive's ``txtcodes/<ID>.txt`` without touching the network."""
    p = txt_path(layout, game_id)
    try:
        return parse_txtcodes(p.read_bytes())
    except (OSError, CheatError):
        return None


def gct_bytes(cheats: Sequence[Cheat],
              values: Optional[Mapping[str, Mapping[str, str | int]]] = None) -> bytes:
    """GCT image for ``cheats``. ``values`` maps cheat name -> placeholder values."""
    values = values or {}
    body = b"".join(c.to_bytes(values.get(c.name)) for c in cheats)
    return GCT_HEADER + body + GCT_FOOTER


def build_gct(layout: DriveLayout, game_id: str, cheats: Sequence[Cheat],
              values: Optional[Mapping[str, Mapping[str, str | int]]] = None, *,
              wiiflow: Optional[bool] = None) -> Path:
    """Write ``codes/<ID>.gct`` (and WiiFlow's ``wiiflow/codes/gct/<ID>.gct``) from the
    selected cheats. Raises :class:`CheatError` for missing placeholder values.
    An empty selection removes the gct (no cheats applied)."""
    gid = game_id.strip().upper()
    if wiiflow is None:
        wiiflow = layout.wiiflow.is_dir()
    targets = [gct_path(layout, gid)] + ([wiiflow_gct_path(layout, gid)] if wiiflow else [])
    if not cheats:
        for t in targets:
            t.unlink(missing_ok=True)
        return targets[0]
    data = gct_bytes(cheats, values)
    for t in targets:
        _write(t, data)
    return targets[0]


def selected_in_gct(cf: CheatFile, gct: bytes) -> list[int]:
    """Indexes of cheats whose (placeholder-free) code bytes appear in ``gct``,
    mirroring USB Loader GX's ``IsCheatIncluded``."""
    body = gct[len(GCT_HEADER):-len(GCT_FOOTER)] if gct.startswith(GCT_HEADER) else b""
    out = []
    for i, c in enumerate(cf.cheats):
        if c.needs_values:
            continue
        b = c.to_bytes()
        pos = body.find(b)
        while pos != -1 and pos % 4:
            pos = body.find(b, pos + 1)
        if b and pos != -1:
            out.append(i)
    return out


@dataclass
class CheatStatus:
    """Cheat files on the drive for one game."""

    game_id: str
    title: str
    txt: Optional[Path]
    gct: Optional[Path]
    count: int


def list_cheats(layout: DriveLayout) -> list[CheatStatus]:
    """Every game with a txtcodes and/or gct file on the drive, sorted by ID."""
    ids: dict[str, CheatStatus] = {}

    def ids_in(folder: Path, ext: str) -> Iterable[tuple[str, Path]]:
        if folder.is_dir():
            for p in folder.iterdir():
                if p.is_file() and p.suffix.lower() == ext:
                    yield p.stem.upper(), p

    for gid, p in ids_in(layout.txtcodes, ".txt"):
        try:
            cf = parse_txtcodes(p.read_bytes())
            title, count = cf.title, len(cf.cheats)
        except (OSError, CheatError):
            title, count = "", 0
        ids[gid] = CheatStatus(gid, title, p, None, count)
    for gid, p in ids_in(layout.codes, ".gct"):
        st = ids.setdefault(gid, CheatStatus(gid, "", None, None, 0))
        st.gct = p
    return [ids[k] for k in sorted(ids)]
