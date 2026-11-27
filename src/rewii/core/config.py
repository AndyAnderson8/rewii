"""User settings, persisted as JSON in the per-OS config directory."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from platformdirs import user_cache_dir, user_config_dir

APP = "ReWii"

CONSOLE_TYPES = ("wii", "vwii", "mini", "family")
REGIONS = ("NTSC-U", "PAL", "NTSC-J", "NTSC-K")
FORWARDER_STYLES = ("cover", "black", "classic")
ONLINE_SERVERS = ("wiimmfi", "altwfc", "off")
COVER_LANGS = ("EN", "US", "FR", "DE", "ES", "IT", "NL", "JA", "KO", "ZH")

# fields whose value must be one of a fixed set ("" allowed where the tuple contains it)
_CHOICES: dict[str, tuple[str, ...]] = {
    "console_type": ("", *CONSOLE_TYPES),
    "console_region": ("", *REGIONS),
    "forwarder_style": FORWARDER_STYLES,
    "online_server": ONLINE_SERVERS,
    "cover_lang": COVER_LANGS,
}


def check_library_path(text: str) -> Path:
    """The library folder for what the user typed. Raises ValueError (plain-language
    message) for an empty or relative path - a relative library would silently
    follow whatever folder ReWii happens to be started from."""
    t = (text or "").strip().strip('"')
    if not t:
        raise ValueError("Enter a folder for the library.")
    p = Path(t).expanduser()
    if not p.is_absolute():
        raise ValueError("The library folder must be a full path (for example "
                         f"{Path.home() / 'ReWii'}).")
    return p


def config_dir() -> Path:
    p = Path(user_config_dir(APP, appauthor=False))
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    p = Path(user_cache_dir(APP, appauthor=False))
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Settings:
    library_dir: str = str(Path.home() / "ReWii")
    # console profile; only needed for system-level features (NUS, themes, NAND)
    console_type: str = ""          # "" = not set yet; see CONSOLE_TYPES
    console_region: str = ""        # see REGIONS
    # a Korean console changed to another region: system updates would brick it (Error 003)
    korean_region_changed: bool = False
    # games
    scrub_games: bool = True        # drop unused sectors + update partition
    # channels (forwarders): "cover" shows the cover art while loading, "black" is a blank screen
    forwarder_style: str = "cover"
    # covers
    cover_lang: str = "EN"
    cover_types: list[str] = field(default_factory=lambda: ["cover3D", "cover", "disc", "coverfullHQ"])
    # online play server written to USB Loader GX: "off" | "wiimmfi" | "altwfc"
    # (WiiLink WFC isn't a loader option - it uses the WiiLink WFC Patcher app)
    online_server: str = "wiimmfi"
    last_drive: str = ""
    wii_ip: str = ""                # last address used for "Send to Wii"
    first_run_done: bool = False

    @property
    def library(self) -> Path:
        return Path(self.library_dir).expanduser()

    # library sub-folders
    @property
    def wii_games_dir(self) -> Path:
        return self.library / "Games" / "Wii"

    @property
    def gc_games_dir(self) -> Path:
        return self.library / "Games" / "GameCube"

    @property
    def wads_dir(self) -> Path:
        return self.library / "WADs"

    @property
    def channels_dir(self) -> Path:
        return self.library / "Channels"

    @property
    def saves_dir(self) -> Path:
        return self.library / "Saves"

    @property
    def drive_backups_dir(self) -> Path:
        return self.library / "Backups" / "Drives"

    @property
    def nand_backups_dir(self) -> Path:
        return self.library / "Backups" / "NAND"

    @property
    def downloads_dir(self) -> Path:
        return self.library / "Downloads"

    @property
    def mods_dir(self) -> Path:
        return self.library / "Mods"

    def ensure_library(self) -> None:
        for p in (self.wii_games_dir, self.gc_games_dir, self.wads_dir, self.channels_dir,
                  self.saves_dir, self.drive_backups_dir, self.nand_backups_dir,
                  self.downloads_dir, self.mods_dir):
            p.mkdir(parents=True, exist_ok=True)

    # persistence
    @classmethod
    def path(cls) -> Path:
        return config_dir() / "settings.json"

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        """Read the settings file. Never raises: a missing, unreadable or damaged
        file gives the defaults, and any single bad value falls back to its default."""
        s = cls()
        try:
            path = path or cls.path()
            if not path.exists():
                return s
            data = json.loads(path.read_text("utf-8"))
        except Exception:  # noqa: BLE001 - a broken settings file must not stop the app
            return s
        if not isinstance(data, dict):
            return s
        for f in fields(cls):
            if f.name in data:
                v = _coerce(f.name, getattr(s, f.name), data[f.name])
                if v is not None:
                    setattr(s, f.name, v)
        return s

    def save(self, path: Path | None = None) -> None:
        """Write the settings file (raises OSError if the config folder isn't writable)."""
        path = path or self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), "utf-8")
        tmp.replace(path)


def _coerce(name: str, default: Any, value: Any) -> Any:
    """``value`` if it is usable for field ``name``, else None (keep the default)."""
    if isinstance(default, bool):
        return value if isinstance(value, bool) else None
    if isinstance(default, list):
        if not isinstance(value, list):
            return None
        return [x for x in value if isinstance(x, str)]
    if not isinstance(value, str):
        return None
    if name in _CHOICES:
        return value if value in _CHOICES[name] else None
    if name == "library_dir":
        try:
            return str(check_library_path(value))
        except ValueError:
            return None
    return value
