"""One shape for "ReWii looked at something and found this".

The drive health check (``health``), the SysCheck report analysis (``syscheck``)
and the WAD risk check (``wad.Risk``) all report a :class:`Severity`, a short
machine-readable code and a plain-language message; health and SysCheck findings
may also name a fix (:class:`Fix`). The UI has one severity -> icon mapping.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Optional


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    @property
    def short(self) -> str:
        """At most five letters, for fixed-width lists (``warn``)."""
        return "warn" if self is Severity.WARNING else self.value

    @property
    def rank(self) -> int:
        """0 for errors, then warnings, then notes (sort key: worst first)."""
        return {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}[self]


class Fix(StrEnum):
    """Every fix a finding can name. Members equal their text (``Fix.SPLIT_FILE == "split_file"``)."""
    # drive health - repaired by health.fix() itself (see SAFE_FIXES)
    FLATTEN_NESTED_APPS = "flatten_nested_apps"
    FLATTEN_APP_FOLDER = "flatten_app_folder"
    MOVE_WII_GAME = "move_wii_game"
    MOVE_GC_GAME = "move_gc_game"
    SPLIT_FILE = "split_file"
    DELETE_MAC_JUNK = "delete_mac_junk"
    DELETE_TEMP_FILES = "delete_temp_files"
    # drive health - an action the UI offers elsewhere
    FORMAT_DRIVE = "format_drive"                   # erase and format FAT32 (formatting)
    INSTALL_STARTER_APPS = "install_starter_apps"   # Drive -> Set up (osc starter kit)
    CONVERT_GAME = "convert_game"                   # convert or re-add the game (games)
    IMPORT_WBFS = "import_wbfs"                     # import from an old WBFS partition
    # SysCheck report - each leads to a guide
    UPDATE_43 = "update_43"
    INSTALL_PRIILOADER = "install_priiloader"
    INSTALL_D2X_CIOS = "install_d2x_cios"
    UPDATE_HBC = "update_hbc"
    INSTALL_HBC = "install_hbc"
    INSTALL_BOOTMII = "install_bootmii"
    RESTORE_IOS = "restore_ios"


#: Fixes ``health.fix`` applies by itself: renames/moves on the same drive, deleting
#: macOS junk and ReWii's own unfinished files, splitting oversized files.
SAFE_FIXES = frozenset({Fix.FLATTEN_NESTED_APPS, Fix.FLATTEN_APP_FOLDER, Fix.MOVE_WII_GAME,
                        Fix.MOVE_GC_GAME, Fix.SPLIT_FILE, Fix.DELETE_MAC_JUNK, Fix.DELETE_TEMP_FILES})


@dataclass
class Finding:
    """One problem (or note), in plain language."""

    id: str                       # machine-readable kind, e.g. "nested_apps"
    severity: Severity            # compares equal to "info" / "warning" / "error"
    message: str                  # plain language for the user
    fix_id: Optional[str] = None  # a Fix member (or None)
    path: Optional[Path] = None   # the file/folder concerned, if any
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.severity = Severity(self.severity)

    @property
    def fix(self) -> str:
        """The fix id, ``""`` when there is none."""
        return self.fix_id or ""

    @property
    def auto_fixable(self) -> bool:
        """True if ``health.fix`` can repair this finding by itself."""
        return self.fix_id in SAFE_FIXES
