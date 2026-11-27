"""The console profile and the tables that go with it - in one place.

Which Wii the user has (type and region) decides which system files are safe:
a wrong Wii Menu, IOS or theme bricks a console. Every module that needs to
turn a console type into a platform, a region into its letter, or a region
into "its" Wii Menu 4.3 reads the tables here (``wad``, ``nus``, ``themes``,
``osc``, ``syscheck`` and the Settings pane), so they can't drift apart.

Terms:

* *console type* - what the user picks in Settings: ``wii`` (original),
  ``family`` (Wii Family Edition), ``vwii`` (Wii U's Wii mode), ``mini``.
* *platform* - what system software it runs: ``wii`` (original and Family
  Edition), ``vwii`` or ``mini``.
* *region* - ``NTSC-U`` / ``PAL`` / ``NTSC-J`` / ``NTSC-K``; its *letter* is the
  suffix of the Wii Menu version (4.3**U**, 4.3**E**, 4.3**J**, 4.3**K**).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

CONSOLE_TYPES = ("wii", "vwii", "mini", "family")
REGIONS = ("NTSC-U", "PAL", "NTSC-J", "NTSC-K")

PLATFORM_WII, PLATFORM_VWII, PLATFORM_MINI = "wii", "vwii", "mini"
PLATFORMS = (PLATFORM_WII, PLATFORM_VWII, PLATFORM_MINI)

#: console type -> platform
PLATFORM_OF = {"wii": PLATFORM_WII, "family": PLATFORM_WII, "vwii": PLATFORM_VWII, "mini": PLATFORM_MINI}
#: platform -> the Open Shop Channel's name for it
OSC_PLATFORM_OF = {PLATFORM_WII: "wii", PLATFORM_VWII: "vwii", PLATFORM_MINI: "wii_mini"}
#: console type -> name used in messages
CONSOLE_LABELS = {"wii": "Wii", "vwii": "Wii U (vWii)", "mini": "Wii mini", "family": "Wii Family Edition"}
#: console type -> name shown in the Settings choice (the plain Wii says "original" there)
CONSOLE_CHOICE_LABELS = {**CONSOLE_LABELS, "wii": "Wii (original)"}

#: region -> Wii Menu version letter, and back
REGION_LETTER = {"NTSC-U": "U", "PAL": "E", "NTSC-J": "J", "NTSC-K": "K"}
REGION_BY_LETTER = {v: k for k, v in REGION_LETTER.items()}
#: low 5 bits of a Wii Menu title version -> region letter (v513 & 0x1F = 1 = U ...)
SYSMENU_LETTER_BY_BITS = {0: "J", 1: "U", 2: "E", 6: "K"}
#: TMD region field -> region name (3 = region free)
TMD_REGIONS = {0: "NTSC-J", 1: "NTSC-U", 2: "PAL", 3: "Region free", 4: "NTSC-K"}

WII_SYSMENU_TITLE = "0000000100000002"
VWII_SYSMENU_TITLE = "0000000700000002"       # where NUS keeps the vWii Wii Menu


@dataclass(frozen=True)
class SystemMenu43:
    """Wii Menu 4.3 for one platform and region."""
    platform: str
    region: str
    version: int            # title version (v513 = 4.3U ...)
    content_id: int         # the menu's main archive, which is also the base file of a theme
    nus_title_id: str       # where NUS serves it

    @property
    def letter(self) -> str:
        return REGION_LETTER[self.region]


#: The only Wii Menus ReWii handles: 4.3 per (platform, region). Versions from NUSGet's
#: database, content ids verified against the NUS TMDs (wii.hacks.guide docs/themes.md
#: for vWii). There is no Korean vWii and no downloadable Wii mini menu.
SYSMENU_43: dict[tuple[str, str], SystemMenu43] = {
    (m.platform, m.region): m for m in (
        SystemMenu43(PLATFORM_WII, "NTSC-J", 512, 0x94, WII_SYSMENU_TITLE),
        SystemMenu43(PLATFORM_WII, "NTSC-U", 513, 0x97, WII_SYSMENU_TITLE),
        SystemMenu43(PLATFORM_WII, "PAL", 514, 0x9A, WII_SYSMENU_TITLE),
        SystemMenu43(PLATFORM_WII, "NTSC-K", 518, 0x9D, WII_SYSMENU_TITLE),
        SystemMenu43(PLATFORM_VWII, "NTSC-J", 608, 0x1C, VWII_SYSMENU_TITLE),
        SystemMenu43(PLATFORM_VWII, "NTSC-U", 609, 0x1F, VWII_SYSMENU_TITLE),
        SystemMenu43(PLATFORM_VWII, "PAL", 610, 0x22, VWII_SYSMENU_TITLE),
    )
}


def platform_of(console_type: str) -> str:
    """Platform of a console type (``""`` if the type is unknown or not set)."""
    return PLATFORM_OF.get(console_type, "")


def console_label(console_type: str) -> str:
    """Name of a console type for a message ("Wii U (vWii)"...)."""
    return CONSOLE_LABELS.get(console_type, console_type or "unknown console")


def osc_platform(name: str) -> str:
    """The Open Shop Channel platform for a console type; OSC's own names
    (``wii`` / ``vwii`` / ``wii_mini``) and unknown text pass through unchanged."""
    plat = PLATFORM_OF.get(name)
    return OSC_PLATFORM_OF[plat] if plat else name


def sysmenu_region(version: int) -> Optional[str]:
    """Region of a Wii Menu title version (513 -> "NTSC-U"), from its low bits."""
    letter = SYSMENU_LETTER_BY_BITS.get(version & 0x1F)
    return REGION_BY_LETTER.get(letter) if letter else None


def same_platform(a: str, b: str) -> bool:
    """True if two console types run the same system software (``wii`` and ``family``)."""
    return bool(platform_of(a)) and platform_of(a) == platform_of(b)


@dataclass(frozen=True)
class ConsoleProfile:
    """The user's console as the system features need it (see ``Settings.profile``)."""
    console_type: str = ""              # one of CONSOLE_TYPES, "" = not set
    region: str = ""                    # one of REGIONS, "" = not set
    korean_region_changed: bool = False  # a Korean console changed to another region

    @property
    def is_set(self) -> bool:
        return bool(self.console_type and self.region)

    @property
    def platform(self) -> str:
        return platform_of(self.console_type)

    @property
    def wii_like(self) -> bool:
        """An original Wii or a Family Edition (same system software)."""
        return self.platform == PLATFORM_WII

    @property
    def osc_platform(self) -> str:
        return osc_platform(self.console_type)

    @property
    def label(self) -> str:
        return console_label(self.console_type)

    @property
    def region_letter(self) -> str:
        return REGION_LETTER.get(self.region, "")

