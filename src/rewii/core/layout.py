"""The ReWii drive layout: where everything lives on the Wii's USB drive (or SD card).

Paths follow what USB Loader GX, WiiFlow Lite, Nintendont, YAWMME and
SaveGame Manager GX expect, per wii.hacks.guide.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .fileops import is_reserved_name, require_drive, write_atomic
from .ids import id_in_brackets

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(title: str, maxlen: int = 64, default: str = "Game") -> str:
    """Make a title safe as a FAT32 file/folder name on every OS: no reserved
    characters, no trailing dot or space, and never a Windows device name
    (``CON``, ``NUL``, ``COM1``... get a leading underscore)."""
    t = _BAD.sub("", title).strip().rstrip(".")
    t = re.sub(r"\s+", " ", t)
    t = t[:maxlen].rstrip(" .") or default
    return "_" + t if is_reserved_name(t) else t


def game_folder_name(title: str, game_id: str) -> str:
    return f"{safe_name(title)} [{game_id}]"


def id_from_folder(name: str) -> str | None:
    """The disc ID in a ``Title [GAMEID]`` folder name (see :mod:`rewii.core.ids`)."""
    return id_in_brackets(name)


# Top-level folder names on the drive. Code outside this module uses the DriveLayout
# properties (or these names, for paths inside archives) - never its own string literals.
APPS_DIR = "apps"
WBFS_DIR = "wbfs"                 # Wii games
GC_GAMES_DIR = "games"            # GameCube games (Nintendont)
WAD_DIR = "wad"
REWII_DIR = "rewii"               # ReWii's own records
THEMES_DIR = "themes"
BOOTMII_DIR = "bootmii"
RIIVOLUTION_DIR = "riivolution"
#: The folders that hold games (what "back up without games" leaves out).
GAME_DIR_NAMES = (WBFS_DIR, GC_GAMES_DIR)
#: Every top-level folder ReWii (or the apps it sets up) writes to.
MANAGED_TOPS = frozenset({APPS_DIR, WBFS_DIR, GC_GAMES_DIR, WAD_DIR, "txtcodes", "codes", "savegames",
                          "saves", "private", RIIVOLUTION_DIR, REWII_DIR, BOOTMII_DIR, "controllers",
                          "wiiflow"})


@dataclass
class DriveLayout:
    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    @classmethod
    def of(cls, drive: DriveLayout | Path | str) -> DriveLayout:
        """``drive`` as a DriveLayout: core functions take a layout, and accept the
        drive's root folder in its place."""
        return drive if isinstance(drive, DriveLayout) else cls(Path(drive))

    # homebrew
    @property
    def apps(self) -> Path:
        return self.root / APPS_DIR

    @property
    def nintendont_dols(self) -> tuple[Path, ...]:
        """Where Nintendont's boot.dol may be (its folder is spelled both ways)."""
        return (self.apps / "Nintendont" / "boot.dol", self.apps / "nintendont" / "boot.dol")

    def app_dir(self, name: str) -> Path:
        return self.apps / name

    # games
    @property
    def wbfs(self) -> Path:
        return self.root / WBFS_DIR

    @property
    def gc_games(self) -> Path:
        return self.root / GC_GAMES_DIR

    def wii_game_dir(self, title: str, game_id: str) -> Path:
        return self.wbfs / game_folder_name(title, game_id)

    def wii_game_file(self, title: str, game_id: str, ext: str = ".wbfs") -> Path:
        return self.wii_game_dir(title, game_id) / f"{game_id}{ext}"

    def gc_game_dir(self, title: str, game_id: str) -> Path:
        return self.gc_games / game_folder_name(title, game_id)

    def gc_game_file(self, title: str, game_id: str, disc: int = 0, ext: str = ".iso") -> Path:
        name = "game" if disc == 0 else f"disc{disc + 1}"
        return self.gc_game_dir(title, game_id) / f"{name}{ext}"

    # USB Loader GX
    @property
    def usbloadergx(self) -> Path:
        return self.apps / "usbloader_gx"

    @property
    def gx_config(self) -> Path:
        return self.usbloadergx / "GXGlobal.cfg"

    @property
    def gx_images(self) -> Path:
        return self.usbloadergx / "images"

    def gx_cover_dir(self, kind: str) -> Path:
        """kind: cover3D | cover | coverfull | coverfullHQ | disc"""
        return {
            "cover3D": self.gx_images,
            "cover": self.gx_images / "2D",
            "coverfull": self.gx_images / "full",
            "coverfullHQ": self.gx_images / "full",
            "disc": self.gx_images / "disc",
        }[kind]

    # WiiFlow Lite
    @property
    def wiiflow(self) -> Path:
        return self.root / "wiiflow"

    @property
    def has_wiiflow(self) -> bool:
        """True if WiiFlow is set up on this drive - then its copies of covers,
        cheats and banners are written too."""
        return self.wiiflow.is_dir()

    @property
    def wiiflow_codes(self) -> Path:          # cheat lists (<ID>.txt)
        return self.wiiflow / "codes"

    @property
    def wiiflow_gct(self) -> Path:            # compiled cheats (<ID>.gct)
        return self.wiiflow_codes / "gct"

    @property
    def wiiflow_covers(self) -> Path:         # flat front covers
        return self.wiiflow / "covers"

    @property
    def wiiflow_boxcovers(self) -> Path:      # full covers
        return self.wiiflow / "boxcovers"


    # cheats
    @property
    def txtcodes(self) -> Path:
        return self.root / "txtcodes"

    @property
    def codes(self) -> Path:
        return self.root / "codes"

    # saves
    @property
    def savegames(self) -> Path:      # SaveGame Manager GX
        return self.root / "savegames"

    @property
    def nintendont_saves(self) -> Path:
        return self.root / "saves"

    @property
    def gcmm_backups(self) -> Path:   # GCMM
        return self.root / "MCBACKUP"

    @property
    def private_saves(self) -> Path:  # Wii Menu Data Management (SD only)
        return self.root / "private" / "wii" / "title"

    # system
    @property
    def wad(self) -> Path:            # YAWMME opens this folder automatically
        return self.root / WAD_DIR

    @property
    def nintendont_cfg(self) -> Path:
        return self.root / "nincfg.bin"

    @property
    def riivolution(self) -> Path:
        return self.root / RIIVOLUTION_DIR

    @property
    def themes(self) -> Path:         # csm-installer
        return self.root / THEMES_DIR

    @property
    def bootmii(self) -> Path:
        return self.root / BOOTMII_DIR

    @property
    def bootmii_ini(self) -> Path:
        return self.bootmii / "bootmii.ini"

    @property
    def nand_dumps(self) -> Path:     # nanddumper@ios output
        return self.root / "wii" / "backups"

    # ReWii's own folder on the drive
    @property
    def rewii(self) -> Path:
        return self.root / REWII_DIR

    @property
    def manifest(self) -> Path:
        return self.rewii / "drive.json"

    def read_manifest(self) -> dict:
        try:
            return json.loads(self.manifest.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def require_root(self) -> Path:
        """The drive's root if it is there, else raise ``DriveNotConnected``. Call this
        before writing: a vanished root must never be re-created on the computer's disk."""
        return require_drive(self.root)

    def connected(self) -> bool:
        try:
            return self.root.is_dir()
        except OSError:
            return False

    def write_manifest(self, data: dict) -> None:
        self.require_root()
        self.rewii.mkdir(parents=True, exist_ok=True)
        cur = self.read_manifest()
        cur.update(data)
        write_atomic(self.manifest, json.dumps(cur, indent=2))

    def ensure_dirs(self) -> None:
        self.require_root()
        for p in (self.apps, self.wbfs, self.gc_games, self.wad, self.txtcodes,
                  self.savegames, self.rewii):
            p.mkdir(parents=True, exist_ok=True)
