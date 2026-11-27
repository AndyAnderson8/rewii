"""Tests for rewii.core.syscheck using real-format SysCheck reports."""
from __future__ import annotations

from pathlib import Path

from rewii.core import syscheck as SC

# SysCheck ME output, PAL Wii (format from a public report; console id changed)
WII_PAL = """\
SysCheck ME v2.5.0 by blackb0x, JoostinOnline, Double_A, R2-D2199 and Nano\r
...runs on IOS58 (rev 6176).\r
\r
Region: PAL\r
System Menu 4.3E (v514)\r
Priiloader installed\r
Drive date: 02.13.2007\r
Homebrew Channel 1.1.2 running on IOS58\r
\r
Hollywood v0x11\r
Console ID: 123456789\r
Console Type: Wii\r
Shop Channel Country: Romania (99)\r
Boot2 v4\r
Found 84 titles.\r
Found 49 IOS on this console. 14 of them are stubs.\r
\r
IOS4 (rev 65280): Stub\r
IOS9 (rev 1034): No Patches\r
IOS38 (rev 4124): No Patches\r
IOS56 (rev 5662): No Patches\r
IOS57 (rev 5919): No Patches\r
IOS58 (rev 6176): USB 2.0\r
IOS224[57] (rev 65535, Info: hermes-v5): Trucha Bug, NAND Access, USB 2.0\r
IOS248[38] (rev 65535, Info: d2x-v11beta1): Trucha Bug, ES Identify, NAND Access\r
IOS249[56] (rev 65535, Info: d2x-v11beta1): Trucha Bug, NAND Access, USB 2.0\r
IOS250[57] (rev 65535, Info: d2x-v11beta1): Trucha Bug, NAND Access, USB 2.0\r
IOS251[58] (rev 65535, Info: d2x-v11beta1): Trucha Bug, NAND Access, USB 2.0\r
IOS254 (rev 65281): BootMii\r
BC v6\r
MIOS v10\r
Report generated on 10/09/2023.\r
"""

# SysCheck ME on vWii without cIOS / Priiloader
VWII = """\
SysCheck ME v2.5.0 by blackb0x, JoostinOnline, Double_A, R2-D2199 and Nano
...runs on vIOS58 (rev 6432).

Region: NTSC-U
System Menu 4.3U (v609)

Drive date: 06.28.2011
Homebrew Channel 1.1.2 running on IOS58

Hollywood v0x21
Console ID: 12345
Console Type: vWii
Shop Channel Country: Canada (18)
Boot2 v0
Found 154 titles.
Found 31 IOS on this console. 0 of them are stubs.

vIOS9 (rev 1290): No Patches
vIOS38 (rev 4380): No Patches
vIOS56 (rev 5918): No Patches
vIOS57 (rev 6175): No Patches
vIOS58 (rev 6432): USB 2.0
vIOS80 (rev 7200): No Patches
Report generated on 01/26/2024.
"""

# HDE report, region-changed, old d2x, Excel-mangled (tabs), HBC old, no BootMii
OLD = """\
SysCheck HDE v2.4.0 HacksDen Edition by JoostinOnline, Double_A, R2-D2199, and Nano
...runs on IOS58 (rev 6176).
Region: NTSC-U (original region: JAP)
System Menu 4.2U (v481)
Homebrew Channel 1.0.8 running on IOS61
Console Type: Wii
Boot2 v4
IOS38 (rev 4124): Stub
IOS58 (rev 6176): USB 2.0
IOS249[57] (rev 21010\t Info: d2x-v10beta52): Trucha Bug\t NAND Access\t USB 2.0
IOS250[56] (rev 65535, Info: d2x-v10beta52): Trucha Bug, NAND Access, USB 2.0
IOS253 (rev 65535): NANDEmu
"""


def test_parse_wii_pal() -> None:
    r = SC.parse_syscheck(WII_PAL)
    assert r.tool.startswith("SysCheck ME v2.5.0")
    assert r.running_ios == 58 and r.region == "PAL" and r.sysmenu == "4.3E"
    assert r.sysmenu_version == 514 and r.console_type == "wii" and r.priiloader
    assert r.hbc_version == (1, 1, 2) and r.hbc_ios == 58 and r.boot2_version == 4
    assert r.hollywood == "0x11" and r.console_id == "123456789" and r.titles == 84
    assert r.bc_version == 6 and r.mios_version == 10 and r.report_date == "10/09/2023"
    assert r.bootmii_ios and r.stubs() == [4]
    c = r.ios[249]
    assert (c.base, c.revision, c.info, c.d2x_version) == (56, 65535, "d2x-v11beta1", 11)
    assert c.patches == ["Trucha Bug", "NAND Access", "USB 2.0"]
    assert r.ios[9].patches == []
    assert set(r.cios()) == {224, 248, 249, 250, 251}
    assert [f for f in r.findings if f.severity != "info"] == []
    assert SC.profile_from_syscheck(r) == ("wii", "PAL")


def test_parse_vwii() -> None:
    r = SC.parse_syscheck(VWII)
    assert r.console_type == "vwii" and r.sysmenu == "4.3U" and not r.priiloader
    assert r.running_ios == 58 and r.ios[58].vwii
    fixes = {f.fix for f in r.findings}
    assert "install_d2x_cios" in fixes and "install_priiloader" in fixes
    assert "install_bootmii" not in fixes and "update_43" not in fixes
    assert SC.profile_from_syscheck(r) == ("vwii", "NTSC-U")


def test_old_report_findings() -> None:
    r = SC.parse_syscheck(OLD)
    assert r.original_region == "JAP" and r.sysmenu == "4.2U"
    assert r.ios[249].revision == 21010 and r.ios[249].info == "d2x-v10beta52"
    assert r.ios[249].patches == ["Trucha Bug", "NAND Access", "USB 2.0"]
    assert r.ios[253].nandemu
    fixes = [f.fix for f in r.findings]
    for fid in ("update_43", "install_priiloader", "install_d2x_cios", "update_hbc",
                "install_bootmii", "restore_ios"):
        assert fid in fixes, fid
    msgs = " ".join(f.message for f in r.findings)
    assert "region-changed" in msgs and "248" in msgs and "base 57 (expected 56)" in msgs


def test_sysmenu_names_and_mini() -> None:
    assert SC.sysmenu_name(513) == "4.3U" and SC.sysmenu_name(518) == "4.3K"
    assert SC.sysmenu_name(450) == "4.1E" and SC.sysmenu_name(610) == "4.3E"
    assert SC.sysmenu_name(4609) == "4.3U" and SC.sysmenu_name(99999) == ""
    r = SC.parse_syscheck("SysCheck ME\nSystem Menu 4.3U (v4609)\nConsole Type: Wii\n"
                          "IOS58 (rev 6176): USB 2.0\n")
    assert r.console_type == "mini"


def test_not_a_report_and_find(tmp_path: Path) -> None:
    r = SC.parse_syscheck("hello\nworld\n")
    assert r.findings[0].severity == "error"
    (tmp_path / "SysCheck.csv").write_bytes(WII_PAL.encode("cp1252"))
    p = SC.find_syscheck(tmp_path)
    assert p is not None and SC.read_syscheck(p).sysmenu == "4.3E"
