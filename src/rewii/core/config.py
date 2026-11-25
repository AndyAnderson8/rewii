"""User settings, persisted as JSON in the per-OS config directory."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir

APP = "ReWii"

CONSOLE_TYPES = ("wii", "vwii", "mini", "family")
REGIONS = ("NTSC-U", "PAL", "NTSC-J", "NTSC-K")


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
    # games
    scrub_games: bool = True        # drop unused sectors + update partition
    # channels (forwarders): "cover" shows the cover art while loading, "black" is a blank screen
    forwarder_style: str = "cover"
    # covers
    cover_lang: str = "EN"
    cover_types: list[str] = field(default_factory=lambda: ["cover3D", "cover", "disc", "coverfullHQ"])
    # online: "" (none), "wiimmfi" or "wiilink" - written to USB Loader GX settings
    online_server: str = "wiilink"
    last_drive: str = ""
    first_run_done: bool = False
    dry_run: bool = False

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
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or cls.path()
        s = cls()
        if path.exists():
            try:
                data = json.loads(path.read_text("utf-8"))
                names = {f.name for f in fields(cls)}
                for k, v in data.items():
                    if k in names:
                        setattr(s, k, v)
            except (OSError, ValueError):
                pass
        return s

    def save(self, path: Path | None = None) -> None:
        path = path or self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), "utf-8")
        tmp.replace(path)
