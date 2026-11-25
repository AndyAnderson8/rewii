"""GameTDB title database (``wiitdb.zip``): titles and metadata for Wii, GameCube,
WiiWare, Virtual Console and channel IDs.

The ~10 MB ``wiitdb.xml`` is downloaded weekly via :func:`rewii.core.net.cached`,
parsed once with a streaming parser and turned into a compact JSON index in the
cache directory. Everything works offline once a cache exists; without one,
:meth:`TitleDB.load` returns an empty database whose lookups return ``None``.
"""
from __future__ import annotations

import json
import unicodedata
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Iterable, Optional
from xml.etree import ElementTree as ET

from . import config, net
from .config import Settings

GAMETDB_URL = ("https://www.gametdb.com/wiitdb.zip?LANG={lang}"
               "&FALLBACK=TRUE&WIIWARE=TRUE&GAMECUBE=TRUE")
"""Full database (Wii + GameCube + WiiWare/VC/channels) with English fallback titles."""

MAX_AGE = 7 * 86400
INDEX_VERSION = 1
SYNOPSIS_LEN = 400
LANGS = ("EN", "JA", "FR", "DE", "ES", "IT", "NL", "PT", "ZHTW", "ZHCN", "KO", "RU")

_TYPE_NAMES = {"": "Wii", "CUSTOM": "Custom"}


@dataclass(frozen=True)
class GameInfo:
    """Metadata for one title. ``type`` is Wii, GameCube, WiiWare, VC-NES (etc.),
    Channel, Homebrew or Custom."""

    id: str
    title: str
    type: str = "Wii"
    region: str = ""
    languages: tuple[str, ...] = ()
    developer: str = ""
    publisher: str = ""
    year: Optional[int] = None
    genre: tuple[str, ...] = ()
    players: int = 0
    wifi_players: int = 0
    controllers: tuple[str, ...] = ()
    required_controllers: tuple[str, ...] = ()
    rating: str = ""
    synopsis: str = ""

    @property
    def is_gamecube(self) -> bool:
        return self.type == "GameCube"

    @property
    def is_wii_disc(self) -> bool:
        return self.type in ("Wii", "Custom")

    @property
    def is_virtual_console(self) -> bool:
        return self.type.startswith("VC")

    @classmethod
    def from_dict(cls, d: dict) -> "GameInfo":
        kw = dict(d)
        for k in ("languages", "genre", "controllers", "required_controllers"):
            kw[k] = tuple(kw.get(k) or ())
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in kw.items() if k in known})

    def to_dict(self) -> dict:
        d = asdict(self)
        # drop empty values to keep the index small
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in d.items()
                if v not in ("", (), None, 0) or k in ("id", "title")}


# --------------------------------------------------------------------------- parsing
def _short(text: str, limit: int = SYNOPSIS_LEN) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:") + "…"


def _int(v: Optional[str]) -> int:
    try:
        return int(v or 0)
    except ValueError:
        return 0


def _split(v: Optional[str]) -> tuple[str, ...]:
    return tuple(x.strip() for x in (v or "").split(",") if x.strip())


def _parse_game(el: ET.Element, lang: str) -> Optional[GameInfo]:
    gid = (el.findtext("id") or "").strip().upper()
    if not gid:
        return None
    titles: dict[str, tuple[str, str]] = {}
    for loc in el.findall("locale"):
        titles[(loc.get("lang") or "").upper()] = (
            (loc.findtext("title") or "").strip(), (loc.findtext("synopsis") or "").strip())
    title, synopsis = "", ""
    for cand in (lang, "EN", *titles.keys()):
        t, s = titles.get(cand, ("", ""))
        if t and not title:
            title = t
        if s and not synopsis:
            synopsis = s
        if title and synopsis:
            break
    if not title:
        title = (el.get("name") or gid).strip()
    year = None
    date = el.find("date")
    if date is not None and _int(date.get("year")):
        year = _int(date.get("year"))
    rating = ""
    r = el.find("rating")
    if r is not None and r.get("type") and r.get("value"):
        rating = f"{r.get('type')} {r.get('value')}"
    wifi = el.find("wi-fi")
    inp = el.find("input")
    controls: list[str] = []
    required: list[str] = []
    if inp is not None:
        for c in inp.findall("control"):
            t = c.get("type") or ""
            if t and t not in controls:
                controls.append(t)
                if (c.get("required") or "").lower() == "true":
                    required.append(t)
    raw_type = (el.findtext("type") or "").strip()
    return GameInfo(
        id=gid,
        title=title,
        type=_TYPE_NAMES.get(raw_type.upper(), raw_type),
        region=(el.findtext("region") or "").strip(),
        languages=_split(el.findtext("languages")),
        developer=(el.findtext("developer") or "").strip(),
        publisher=(el.findtext("publisher") or "").strip(),
        year=year,
        genre=_split(el.findtext("genre")),
        players=_int(inp.get("players")) if inp is not None else 0,
        wifi_players=_int(wifi.get("players")) if wifi is not None else 0,
        controllers=tuple(controls),
        required_controllers=tuple(required),
        rating=rating,
        synopsis=_short(synopsis),
    )


def parse_wiitdb(source: IO[bytes] | Path | str, lang: str = "EN") -> dict[str, GameInfo]:
    """Stream-parse a ``wiitdb.xml`` file object or path into ``{id: GameInfo}``.

    Titles and synopses use ``lang`` when present, falling back to English.
    """
    lang = lang.upper()
    out: dict[str, GameInfo] = {}
    for _event, el in ET.iterparse(source, events=("end",)):
        if el.tag != "game":
            continue
        info = _parse_game(el, lang)
        if info is not None:
            out[info.id] = info
        el.clear()
    return out


def parse_wiitdb_zip(path: Path, lang: str = "EN") -> dict[str, GameInfo]:
    """Parse the ``wiitdb.xml`` inside a ``wiitdb.zip``."""
    with zipfile.ZipFile(path) as z:
        name = next((n for n in z.namelist() if n.lower().endswith(".xml")), None)
        if name is None:
            raise ValueError(f"{path.name} has no XML file")
        with z.open(name) as f:
            return parse_wiitdb(f, lang)


# --------------------------------------------------------------------------- database
def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


@dataclass
class TitleDB:
    """In-memory title database. Build with :meth:`load`."""

    games: dict[str, GameInfo] = field(default_factory=dict)
    version: str = ""
    lang: str = "EN"

    # -- construction
    @staticmethod
    def index_path(lang: str = "EN") -> Path:
        return config.cache_dir() / "gametdb" / f"index-{lang.upper()}.json"

    @staticmethod
    def zip_cache_name(lang: str = "EN") -> str:
        return f"gametdb/wiitdb-{lang.upper()}.zip"

    @classmethod
    def load(cls, settings: Optional[Settings] = None, refresh: bool = False,
             lang: Optional[str] = None) -> "TitleDB":
        """Load the database, downloading/rebuilding as needed.

        Never raises for network or parse problems: falls back to the existing
        index, or returns an empty database (``db.available`` is False).
        """
        lang = (lang or (settings.cover_lang if settings else "") or "EN").upper()
        idx = cls.index_path(lang)
        try:
            zpath: Optional[Path] = net.cached(GAMETDB_URL.format(lang=lang),
                                               cls.zip_cache_name(lang), MAX_AGE, refresh)
        except (net.NetError, OSError):
            zpath = None
        stamp = ""
        if zpath is not None and zpath.exists():
            st = zpath.stat()
            stamp = f"{st.st_size}:{int(st.st_mtime)}"
        db = cls._read_index(idx, lang)
        if db is not None and (zpath is None or db.version == stamp):
            return db
        if zpath is not None:
            try:
                games = parse_wiitdb_zip(zpath, lang)
            except (zipfile.BadZipFile, ET.ParseError, ValueError, OSError):
                games = {}
            if games:
                db = cls(games, stamp, lang)
                db._write_index(idx)
                return db
        return db if db is not None else cls(lang=lang)

    @classmethod
    def from_games(cls, games: Iterable[GameInfo], lang: str = "EN") -> "TitleDB":
        return cls({g.id: g for g in games}, "", lang)

    @classmethod
    def _read_index(cls, path: Path, lang: str) -> Optional["TitleDB"]:
        try:
            data = json.loads(path.read_text("utf-8"))
            if data.get("format") != INDEX_VERSION:
                return None
            games = {k: GameInfo.from_dict(v) for k, v in data["games"].items()}
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return cls(games, data.get("version", ""), lang)

    def _write_index(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            data = {"format": INDEX_VERSION, "version": self.version, "lang": self.lang,
                    "games": {k: g.to_dict() for k, g in self.games.items()}}
            tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
            tmp.replace(path)
        except OSError:
            pass

    # -- queries
    @property
    def available(self) -> bool:
        return bool(self.games)

    def __len__(self) -> int:
        return len(self.games)

    def __contains__(self, game_id: object) -> bool:
        return isinstance(game_id, str) and self.get(game_id) is not None

    def get(self, game_id: str) -> Optional[GameInfo]:
        """Look up an ID6 (disc) or ID4 (channel/WiiWare); falls back from ID6 to ID4."""
        gid = (game_id or "").strip().upper()
        g = self.games.get(gid)
        if g is None and len(gid) > 4:
            g = self.games.get(gid[:4])
        return g

    def title(self, game_id: str, fallback: str = "") -> str:
        """Title for ``game_id`` or ``fallback`` (default: the ID itself) when unknown."""
        g = self.get(game_id)
        if g is not None:
            return g.title
        return fallback or game_id

    def search(self, text: str, limit: int = 50, types: Optional[Iterable[str]] = None
               ) -> list[GameInfo]:
        """Case/accent-insensitive search on ID and title; every word must match.

        Results: exact ID, then title prefix, then other matches, alphabetically.
        """
        q = _fold(text.strip())
        if not q:
            return []
        words = q.split()
        want = set(types) if types else None
        ranked: list[tuple[int, str, GameInfo]] = []
        qid = q.upper()
        for gid, g in self.games.items():
            if want is not None and g.type not in want:
                continue
            ft = _fold(g.title)
            if gid == qid:
                rank = 0
            elif gid.startswith(qid) and len(words) == 1:
                rank = 1
            elif all(w in ft for w in words):
                rank = 2 if ft.startswith(q) else 3
            else:
                continue
            ranked.append((rank, ft, g))
        ranked.sort(key=lambda t: (t[0], t[1], t[2].id))
        return [g for _, _, g in ranked[:limit]]
