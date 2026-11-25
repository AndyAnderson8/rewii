"""The ReWii drive layout: where everything lives on the Wii's USB drive (or SD card).

Paths follow what USB Loader GX, WiiFlow Lite, Nintendont, YAWMME and
SaveGame Manager GX expect, per wii.hacks.guide.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(title: str, maxlen: int = 64) -> str:
    """Make a title safe for FAT32 folder names on every OS."""
    t = _BAD.sub("", title).strip().rstrip(".")
    t = re.sub(r"\s+", " ", t)
    return (t[:maxlen].rstrip() or "Game")


def game_folder_name(title: str, game_id: str) -> str:
    return f"{safe_name(title)} [{game_id}]"


_ID_IN_NAME = re.compile(r"\[([A-Z0-9]{6})\]")


def id_from_folder(name: str) -> str | None:
    m = _ID_IN_NAME.search(name)
    return m.group(1) if m else None


@dataclass
class DriveLayout:
    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    # homebrew
    @property
    def apps(self) -> Path:
        return self.root / "apps"

    def app_dir(self, name: str) -> Path:
        return self.apps / name

    # games
    @property
    def wbfs(self) -> Path:
        return self.root / "wbfs"

    @property
    def gc_games(self) -> Path:
        return self.root / "games"

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
        return self.root / "wad"

    @property
    def nintendont_cfg(self) -> Path:
        return self.root / "nincfg.bin"

    @property
    def riivolution(self) -> Path:
        return self.root / "riivolution"

    @property
    def bootmii(self) -> Path:
        return self.root / "bootmii"

    @property
    def nand_dumps(self) -> Path:     # nanddumper@ios output
        return self.root / "wii" / "backups"

    @property
    def syscheck_csv(self) -> Path:
        return self.root / "syscheck.csv"

    # ReWii's own folder on the drive
    @property
    def rewii(self) -> Path:
        return self.root / "rewii"

    @property
    def manifest(self) -> Path:
        return self.rewii / "drive.json"

    def is_rewii_drive(self) -> bool:
        return self.manifest.exists()

    def read_manifest(self) -> dict:
        try:
            return json.loads(self.manifest.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def write_manifest(self, data: dict) -> None:
        self.rewii.mkdir(parents=True, exist_ok=True)
        cur = self.read_manifest()
        cur.update(data)
        self.manifest.write_text(json.dumps(cur, indent=2), "utf-8")

    def ensure_dirs(self) -> None:
        for p in (self.apps, self.wbfs, self.gc_games, self.wad, self.txtcodes,
                  self.savegames, self.rewii):
            p.mkdir(parents=True, exist_ok=True)
