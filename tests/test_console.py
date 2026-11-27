"""The console tables are defined once (core/console.py); other modules only hold views."""
from __future__ import annotations

from rewii.core import console, nus, osc, syscheck, themes, wad
from rewii.core.config import CONSOLE_TYPES, REGIONS, Settings


def test_profile_properties():
    p = Settings(console_type="family", console_region="PAL").profile
    assert (p.platform, p.wii_like, p.label, p.region_letter, p.osc_platform) == (
        "wii", True, "Wii Family Edition", "E", "wii")
    assert p.is_set and console.SYSMENU_43[(p.platform, p.region)].version == 514
    mini = console.ConsoleProfile("mini", "NTSC-U")
    assert (mini.platform, mini.osc_platform) == ("mini", "wii_mini")
    assert ("vwii", "NTSC-K") not in console.SYSMENU_43 and ("mini", "PAL") not in console.SYSMENU_43
    assert not console.ConsoleProfile().is_set and console.ConsoleProfile().platform == ""
    assert Settings(korean_region_changed=True).profile.korean_region_changed


def test_every_console_type_and_region_is_covered():
    assert set(console.PLATFORM_OF) == set(CONSOLE_TYPES) == set(console.CONSOLE_LABELS)
    assert set(console.REGION_LETTER) == set(REGIONS)
    assert set(console.OSC_PLATFORM_OF) == set(console.PLATFORMS)
    assert set(osc.PLATFORMS) == set(console.OSC_PLATFORM_OF.values())


def test_other_modules_hold_views_of_the_one_table():
    for (plat, region), m in console.SYSMENU_43.items():
        assert wad.SYSTEM_MENU_43[(plat, region)] == m.version
        assert wad.system_menu_region(m.version) == region
        assert themes.MENU_43_CONTENT[m.content_id] == (plat, m.letter, m.version)
        assert syscheck.sysmenu_name(m.version) == f"4.3{m.letter}"
    assert nus.SYSTEM_MENU_43 == {"NTSC-J": 512, "NTSC-U": 513, "PAL": 514, "NTSC-K": 518}
    assert nus.system_menu_43("PAL").filename == "SystemMenu-4.3E-v514.wad"
    assert osc.PLATFORM_BY_CONSOLE["mini"] == "wii_mini" and osc.PLATFORM_BY_CONSOLE["family"] == "wii"
