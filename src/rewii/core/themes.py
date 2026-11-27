"""Wii Menu theme (``.csm``) helpers: identify, validate and stage for csm-installer.

A ``.csm`` is a modified copy of the System Menu's main U8 archive content.
Identification follows csm-installer (github.com/Naim2000/csm-installer,
``source/theme.c``):

* it must be U8 (magic ``55 AA 38 2D``) and contain ``/www.arc``,
  ``/layout/common/health.ash`` and ``/sound/IplSound.brsar``;
* region: ``/html/<R>2/iplsetting.ash`` and ``/FINAL/<R>2/main.sel`` where
  ``<R>2`` is ``US2``/``EU2``/``JP2``/``KR2``;
* platform + major version: from the build path string inside ``main.sel``
  (``...\\ipl\\bin\\RVL\\Final_<R>`` for Wii, ``Compat_irdrepo\\ipl\\Compat`` for
  vWii, ``neceCheck\\WiiMenu`` for Wii mini).

csm-installer browses ``<sd|usb>:/themes/`` (falling back to the root), so
themes are staged in ``/themes/`` on the drive.

The 4.3 main-archive content ids below were read from the NUS TMDs
(``0000000100000002`` v512/513/514/518 and ``0000000700000002`` v608/609/610).
"""
from __future__ import annotations

import re
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .fileops import require_drive

U8_MAGIC = 0x55AA382D
THEMES_DIR = "themes"

# System Menu 4.3 main archive content id -> (platform, region letter, title version)
MENU_43_CONTENT: dict[int, tuple[str, str, int]] = {
    0x94: ("wii", "J", 512), 0x97: ("wii", "U", 513),
    0x9A: ("wii", "E", 514), 0x9D: ("wii", "K", 518),
    0x1C: ("vwii", "J", 608), 0x1F: ("vwii", "U", 609), 0x22: ("vwii", "E", 610),
}
# older menus (for identifying original .app dumps by name)
MENU_OLDER_CONTENT: dict[int, tuple[str, str, str]] = {
    0x84: ("wii", "J", "4.2"), 0x87: ("wii", "U", "4.2"), 0x8A: ("wii", "E", "4.2"),
    0x8D: ("wii", "K", "4.2"), 0x78: ("wii", "J", "4.1"), 0x7B: ("wii", "U", "4.1"),
    0x7E: ("wii", "E", "4.1"), 0x81: ("wii", "K", "4.1"),
}
REGION_DIRS = {"U": "US2", "E": "EU2", "J": "JP2", "K": "KR2"}
REGION_NAMES = {"U": "NTSC-U", "E": "PAL", "J": "NTSC-J", "K": "NTSC-K"}
LETTER_FOR_REGION = {v: k for k, v in REGION_NAMES.items()}
PLATFORM_FOR_CONSOLE = {"wii": "wii", "vwii": "vwii", "mini": "mini", "family": "wii"}
REQUIRED_FILES = ("/www.arc", "/layout/common/health.ash", "/sound/IplSound.brsar")
SIGNATURES = ((0x100, b"Wii_Themer"), (0xF0, b"ModMii_________\xa9______XFlak"),
              (0xF0, b"wiithemer"))


class ThemeError(Exception):
    """A theme file is invalid or doesn't fit the console."""


# --------------------------------------------------------------------------- U8
@dataclass
class U8Entry:
    """One file in a U8 archive."""
    path: str          # absolute, '/'-separated
    offset: int
    size: int


def parse_u8(data: bytes) -> dict[str, U8Entry]:
    """List the files of a U8 archive (paths are lower-case-insensitive keys kept as-is)."""
    if len(data) < 0x20 or struct.unpack_from(">I", data, 0)[0] != U8_MAGIC:
        raise ThemeError("not a U8 archive")
    root_off = struct.unpack_from(">I", data, 4)[0]
    try:
        total = struct.unpack_from(">I", data, root_off + 8)[0]
        str_base = root_off + total * 12
        files: dict[str, U8Entry] = {}
        # stack of (end_index, path)
        stack: list[tuple[int, str]] = [(total, "")]
        i = 1
        while i < total:
            while stack and i >= stack[-1][0]:
                stack.pop()
            typ, name_off, a, b = struct.unpack_from(">BxHII", data, root_off + i * 12)
            name_off |= data[root_off + i * 12 + 1] << 16
            end = data.index(b"\x00", str_base + name_off)
            name = data[str_base + name_off:end].decode("latin-1")
            parent = stack[-1][1] if stack else ""
            path = f"{parent}/{name}"
            if typ == 1:
                stack.append((b, path))
            else:
                files[path] = U8Entry(path, a, b)
            i += 1
    except (struct.error, ValueError, IndexError) as e:
        raise ThemeError(f"corrupt U8 archive: {e}") from e
    return files


# --------------------------------------------------------------------------- identify
@dataclass
class ThemeInfo:
    """What :func:`identify_theme` found out about a ``.csm``/``.app``."""
    path: Path
    is_u8: bool
    is_theme: bool
    regions: list[str] = field(default_factory=list)    # region letters present (U/E/J/K)
    platform: str = ""                                  # "wii" | "vwii" | "mini" | ""
    major: Optional[int] = None                         # menu major version (4)
    minor: Optional[int] = None
    signed: str = ""                                    # theme tool signature, "" if none
    content_id: Optional[int] = None                    # from the file name, if it's NNNNNNNN.app
    problems: list[str] = field(default_factory=list)

    @property
    def region(self) -> str:
        """Single region letter, or '' if ambiguous/unknown."""
        return self.regions[0] if len(self.regions) == 1 else ""

    @property
    def region_name(self) -> str:
        return REGION_NAMES.get(self.region, "")

    @property
    def version_label(self) -> str:
        if self.major is None:
            return ""
        v = f"{self.major}.{self.minor}" if self.minor is not None else f"{self.major}.x"
        return v + self.region


_WII_BUILD = re.compile(rb"(\d)_(\d)\\ipl\\bin\\RVL\\Final_([A-Z])")
_VWII_BUILD = re.compile(rb"Compat_irdrepo\\ipl\\Compat")
_MINI_BUILD = re.compile(rb"neceCheck\\WiiMenu\\ipl\\bin\\RVL\\Final_([A-Z])")


def identify_theme(path: Path) -> ThemeInfo:
    """Identify a theme file's region, platform and menu version."""
    path = Path(path)
    data = path.read_bytes()
    info = ThemeInfo(path, False, False)
    m = re.fullmatch(r"([0-9a-fA-F]{8})\.app", path.name)
    if m:
        info.content_id = int(m.group(1), 16)
    try:
        files = parse_u8(data)
    except ThemeError as e:
        info.problems.append(str(e))
        return info
    info.is_u8 = True
    lower = {k.lower(): v for k, v in files.items()}
    missing = [f for f in REQUIRED_FILES if f.lower() not in lower]
    info.is_theme = not missing
    if missing:
        info.problems.append("not a Wii Menu theme (missing " + ", ".join(missing) + ")")
    sel: Optional[U8Entry] = None
    for letter, d in REGION_DIRS.items():
        ipl = lower.get(f"/html/{d}/iplsetting.ash".lower())
        s = lower.get(f"/final/{d}/main.sel".lower())
        if ipl and s:
            info.regions.append(letter)
            sel = sel or s
    if not info.regions and info.is_theme:
        info.problems.append("couldn't tell which region this theme is for")
    if sel is not None:
        body = data[sel.offset:sel.offset + sel.size]
        if _MINI_BUILD.search(body):
            info.platform, info.major = "mini", 4
        elif _VWII_BUILD.search(body):
            info.platform, info.major, info.minor = "vwii", 4, 3
        elif (mw := _WII_BUILD.search(body)):
            info.platform = "wii"
            info.major, info.minor = int(mw.group(1)), int(mw.group(2))
        for off, sig in SIGNATURES:
            if body[off:off + len(sig)] == sig:
                info.signed = sig.split(b"_")[0].decode("latin-1")
                break
    # The file name (NNNNNNNN.app) is only a hint for the platform/version when the
    # contents don't say. The region is never taken from the name: it decides whether
    # a theme bricks the Wii Menu, so it has to be proven by the archive itself.
    if info.content_id is not None and info.content_id in MENU_43_CONTENT and not info.platform:
        plat, _letter, _v = MENU_43_CONTENT[info.content_id]
        info.platform, info.major, info.minor = plat, 4, 3
    return info


def content_for(console_type: str, region: str) -> Optional[int]:
    """The 4.3 main archive content id for a console profile (config terms), if known."""
    plat = PLATFORM_FOR_CONSOLE.get(console_type, "")
    letter = LETTER_FOR_REGION.get(region, "")
    for cid, (p, r, _v) in MENU_43_CONTENT.items():
        if p == plat and r == letter:
            return cid
    return None


# --------------------------------------------------------------------------- stage
@dataclass
class StagedTheme:
    """Result of :func:`stage_theme`."""
    dest: Path
    info: ThemeInfo
    warnings: list[str]


def check_theme(info: ThemeInfo, console_type: str, region: str) -> list[str]:
    """Reasons this theme must NOT be installed on the profile (empty = OK).

    ``console_type``/``region`` use :mod:`config` values (``wii``/``vwii``/``mini``,
    ``NTSC-U``/``PAL``/``NTSC-J``/``NTSC-K``).
    """
    errs: list[str] = []
    if not info.is_u8:
        errs.append("this file isn't a U8 archive, so it isn't a theme")
        return errs
    if not info.is_theme:
        errs.append("this archive isn't a Wii Menu theme")
    if not region or not console_type:
        errs.append("set your console type and region first (or import a SysCheck report)")
        return errs
    want = LETTER_FOR_REGION.get(region, "")
    if not info.regions:
        errs.append("the theme's region couldn't be identified")
    elif want not in info.regions:
        have = ", ".join(REGION_NAMES.get(r, r) for r in info.regions)
        errs.append(f"this theme is for {have}, but your console is {region}; installing it "
                    "would brick the Wii Menu")
    plat = PLATFORM_FOR_CONSOLE.get(console_type, console_type)
    if info.platform and info.platform != plat:
        errs.append(f"this theme is for {info.platform}, but your console is {console_type}")
    if info.major is not None and info.major != 4:
        errs.append(f"this theme is for System Menu {info.major}.x; update to 4.3 first or "
                    "pick a 4.3 theme")
    elif info.major == 4 and info.minor is not None and info.minor != 3:
        errs.append(f"this theme is for System Menu 4.{info.minor}; ReWii only stages 4.3 themes "
                    "(update to 4.3 first or pick a 4.3 theme)")
    return errs


def stage_theme(theme: Path, drive_root: Path, *, console_type: str, region: str,
                overwrite: bool = False) -> StagedTheme:
    """Copy a checked theme to ``<drive>/themes/`` for csm-installer.

    Raises :class:`ThemeError` if the theme doesn't match the console profile.
    A ``.app`` is staged as-is (csm-installer accepts both extensions).
    """
    theme = Path(theme)
    info = identify_theme(theme)
    errs = check_theme(info, console_type, region)
    if errs:
        raise ThemeError("; ".join(errs))
    warnings = []
    if not info.signed:
        warnings.append("This theme isn't signed by a known theme tool; csm-installer will only "
                        "install it if Priiloader is installed.")
    if info.platform == "" or info.major is None:
        warnings.append("The theme's menu version couldn't be confirmed; it must be a 4.3 theme.")
    warnings.append("Install Priiloader and make a NAND backup before installing themes.")
    dest_dir = require_drive(drive_root) / THEMES_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / theme.name
    if dest.exists():
        if dest.resolve() == theme.resolve():
            return StagedTheme(dest, info, warnings)        # already staged
        if not overwrite and dest.read_bytes() != theme.read_bytes():
            raise ThemeError(f"{dest.name} already exists in themes/ and is different")
    tmp = dest.with_name(dest.name + ".part")
    try:
        shutil.copyfile(theme, tmp)
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return StagedTheme(dest, info, warnings)


def list_staged_themes(drive_root: Path) -> list[ThemeInfo]:
    """Themes waiting in ``<drive>/themes/`` (backup sub-folders excluded)."""
    d = Path(drive_root) / THEMES_DIR
    if not d.is_dir():
        return []
    return [identify_theme(p) for p in sorted(d.iterdir())
            if p.is_file() and p.suffix.lower() in (".csm", ".app") and not p.name.startswith("._")]
