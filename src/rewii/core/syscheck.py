"""Parse SysCheck / SysCheck ModMii Edition reports (``SysCheck.csv``).

Despite the name the file is plain text, one item per line (CRLF). Format per
SysCheck ME source (github.com/modmii/SysCheck-ModMii-Edition, ``source/sysCheck.c``)
and real reports, e.g.::

    SysCheck ME v2.5.0 by blackb0x, JoostinOnline, Double_A, R2-D2199 and Nano
    ...runs on IOS58 (rev 6176).
    Region: PAL
    System Menu 4.3E (v514)
    Priiloader installed
    Homebrew Channel 1.1.2 running on IOS58
    Console Type: Wii
    IOS249[56] (rev 65535, Info: d2x-v11beta1): Trucha Bug, NAND Access, USB 2.0
    IOS254 (rev 65281): BootMii

Reports are localised (German/French/Italian/Spanish), so parsing keys off
language-neutral tokens where possible. Files re-saved by Excel may use tabs
instead of commas; both are accepted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import console
from .findings import Finding, Fix, Severity
from .layout import DriveLayout

# --------------------------------------------------------------------------- versions
_REGION_LETTER = console.SYSMENU_LETTER_BY_BITS      # low bits of the menu version -> letter
_LETTER_TO_REGION = console.REGION_BY_LETTER
_MAJOR = {1: "1.0", 2: "1.0", 3: "2.0", 4: "2.0", 5: "2.1", 6: "2.2", 7: "3.0", 8: "3.1",
          9: "3.2", 10: "3.3", 11: "3.3", 12: "3.4", 13: "4.0", 14: "4.1", 15: "4.2", 16: "4.3"}
# explicit table (wiibrew "System Menu"); keys are title versions
SYSMENU_VERSIONS: dict[int, str] = {
    64: "1.0J", 33: "1.0U", 34: "1.0E", 128: "2.0J", 97: "2.0U", 130: "2.0E", 162: "2.1E",
    192: "2.2J", 193: "2.2U", 194: "2.2E", 224: "3.0J", 225: "3.0U", 226: "3.0E",
    256: "3.1J", 257: "3.1U", 258: "3.1E", 288: "3.2J", 289: "3.2U", 290: "3.2E",
    352: "3.3J", 353: "3.3U", 354: "3.3E", 326: "3.3K", 384: "3.4J", 385: "3.4U", 386: "3.4E",
    390: "3.5K", 416: "4.0J", 417: "4.0U", 418: "4.0E", 448: "4.1J", 449: "4.1U", 450: "4.1E",
    454: "4.1K", 480: "4.2J", 481: "4.2U", 482: "4.2E", 486: "4.2K", 512: "4.3J", 513: "4.3U",
    514: "4.3E", 518: "4.3K",
    # vWii (Wii U) and Wii mini builds of 4.3
    544: "4.3J", 545: "4.3U", 546: "4.3E", 608: "4.3J", 609: "4.3U", 610: "4.3E",
    4609: "4.3U", 4610: "4.3E",
}
VWII_SYSMENU = {544, 545, 546, 608, 609, 610}
MINI_SYSMENU = {4609, 4610}

# expected d2x cIOS slots per wii.hacks.guide: slot -> base IOS
EXPECTED_CIOS = {248: 38, 249: 56, 250: 57, 251: 58}
D2X_MIN_VERSION = 11
HBC_LATEST = (1, 1, 2)
_KOREAN_NAMES = {"KOR", "KOREA", "KOREAN", "NTSC-K", "K"}


def sysmenu_name(version: int) -> str:
    """Map a System Menu title version to e.g. ``4.3U`` ('' if unknown)."""
    if version in SYSMENU_VERSIONS:
        return SYSMENU_VERSIONS[version]
    if version in (54448, 54449, 54450, 54454):      # mauifrog custom 4.1
        return "4.1" + _REGION_LETTER.get(version & 0x1F, "?")
    major = _MAJOR.get(version >> 5)
    letter = _REGION_LETTER.get(version & 0x1F)
    return f"{major}{letter}" if major and letter else ""


# --------------------------------------------------------------------------- model
@dataclass
class IosInfo:
    """One IOS line of the report."""
    slot: int
    revision: Optional[int] = None
    base: Optional[int] = None        # cIOS base IOS, e.g. 56 for IOS249[56]
    info: str = ""                    # e.g. "d2x-v11beta1", "hermes-v5"
    patches: list[str] = field(default_factory=list)
    stub: bool = False
    vwii: bool = False                # printed as vIOS
    bootmii: bool = False
    nandemu: bool = False
    skipped: bool = False

    @property
    def d2x_version(self) -> Optional[int]:
        m = re.search(r"d2xl?-v(\d+)", self.info, re.I)
        return int(m.group(1)) if m else None

    @property
    def is_d2x(self) -> bool:
        return "d2x" in self.info.lower()


def _note(severity: str, message: str, fix: str = "") -> Finding:
    """A SysCheck finding; its id is the fix it suggests (``syscheck_note`` when none)."""
    return Finding(fix or "syscheck_note", Severity(severity), message, fix or None)


@dataclass
class SysCheckReport:
    """Parsed SysCheck report."""
    tool: str = ""
    running_ios: Optional[int] = None
    region: str = ""                  # as printed: NTSC-U / PAL / NTSC-J / KOR
    original_region: str = ""
    sysmenu_version: Optional[int] = None
    sysmenu: str = ""                 # e.g. "4.3U"
    console_type: str = ""            # "wii" | "vwii" | "mini" | ""
    priiloader: bool = False
    hbc_version: Optional[tuple[int, int, int]] = None
    hbc_installed: Optional[bool] = None
    hbc_ios: Optional[int] = None
    hollywood: str = ""
    console_id: str = ""
    country: str = ""
    boot2_version: Optional[int] = None
    titles: Optional[int] = None
    ios: dict[int, IosInfo] = field(default_factory=dict)
    bc_version: Optional[int] = None
    mios_version: Optional[int] = None
    mios_info: str = ""
    report_date: str = ""
    findings: list[Finding] = field(default_factory=list)

    @property
    def bootmii_ios(self) -> bool:
        """BootMii installed as IOS254."""
        i = self.ios.get(254)
        return bool(i and i.bootmii)

    @property
    def bootmii_boot2(self) -> Optional[bool]:
        """Heuristic: SysCheck reports boot2 v65535 when BootMii is boot2 (unverified)."""
        if self.boot2_version is None:
            return None
        return self.boot2_version == 65535

    @property
    def korean_region_changed(self) -> bool:
        """A Korean console whose region was changed to something else. Wii Menu
        4.2+ bricks these with "Error 003", so system updates must not be offered."""
        orig = self.original_region.strip().upper()
        if orig not in _KOREAN_NAMES:
            return False
        now = self.region.strip().upper()
        return now not in _KOREAN_NAMES and not (not now and self.region_letter == "K")

    @property
    def region_letter(self) -> str:
        return self.sysmenu[-1:] if self.sysmenu else ""

    def cios(self) -> dict[int, IosInfo]:
        """Entries in the cIOS slots 248-251 (and any with a base IOS)."""
        return {s: i for s, i in sorted(self.ios.items())
                if s in EXPECTED_CIOS or i.base is not None}

    def stubs(self) -> list[int]:
        return sorted(s for s, i in self.ios.items() if i.stub)


# --------------------------------------------------------------------------- parsing
_IOS_LINE = re.compile(
    r"^(?P<v>v?)IOS(?P<slot>\d+)(?:\[(?P<base>[\d+]+)\])?"
    r"(?:\s*\((?:rev|Rev)\s*(?P<rev>\d+)(?:[,\t]\s*Info:\s*(?P<info>[^)]*))?\))?"
    r"\s*:?\s*(?P<rest>.*)$")
_SYSMENU = re.compile(r"(\d\.\d)(?:/[\d./]+)?([UEJKX])?\s*\(v(\d+)")
_HBC = re.compile(r"Homebrew Channel\s+(\d+)\.(\d+)(?:\.(\d+))?.*?IOS\s*(\d+)", re.I)


def _norm(line: str) -> str:
    line = line.replace("\t", ", ").strip().strip('"').strip()
    return re.sub(r",\s*,", ",", line)


def parse_syscheck(text: str) -> SysCheckReport:
    """Parse the text of a SysCheck report and compute :attr:`findings`."""
    r = SysCheckReport()
    lines = [_norm(x) for x in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [x for x in lines if x]
    if lines and re.search(r"sys ?check", lines[0], re.I):
        r.tool = lines[0]
    for line in lines:
        low = line.lower()
        m = re.match(r"^\.\.\..*?(v?)IOS(\d+)", line)
        if m:
            r.running_ios = int(m.group(2))
            continue
        m = _IOS_LINE.match(line)
        if m and (m.group("rev") or m.group("rest")):
            r.ios[int(m.group("slot"))] = _parse_ios(m)
            continue
        m = re.match(r"^BC v(\d+)", line)
        if m:
            r.bc_version = int(m.group(1))
            continue
        m = re.match(r"^MIOS v(\d+)\s*(.*)$", line)
        if m:
            r.mios_version, r.mios_info = int(m.group(1)), m.group(2).strip(" ()")
            continue
        m = _SYSMENU.search(line)
        if m and ("menu" in low or "système" in low or "systeme" in low or "sistema" in low):
            r.sysmenu_version = int(m.group(3))
            r.sysmenu = sysmenu_name(r.sysmenu_version) or (m.group(1) + (m.group(2) or ""))
            continue
        if "homebrew channel" in low:
            hm = _HBC.search(line)
            if hm:
                r.hbc_installed = True
                r.hbc_version = (int(hm.group(1)), int(hm.group(2)), int(hm.group(3) or 0))
                r.hbc_ios = int(hm.group(4))
            elif "not" in low or "nicht" in low or "pas" in low or "no " in low:
                r.hbc_installed = False
            continue
        if "priiloader" in low or "prefiix" in low:
            r.priiloader = not ("not" in low or "nicht" in low or "pas " in low)
            continue
        m = re.search(r"Hollywood\s+v?(0x[0-9a-fA-F]+)", line)
        if m:
            r.hollywood = m.group(1)
            continue
        m = re.search(r"Boot2\s+v(\d+)", line)
        if m:
            r.boot2_version = int(m.group(1))
            continue
        if ":" in line and _parse_keyed_line(r, line):
            continue
        m = re.search(r"(\d+)\s+(?:titles|Titel|titres|titoli|títulos)", line, re.I)
        if m and r.titles is None and "ios" not in low:
            r.titles = int(m.group(1))
            continue
        m = re.search(r"(\d{1,4}[./]\d{1,2}[./]\d{2,4})", line)
        if m and line is lines[-1]:
            r.report_date = m.group(1)
    _refine_console_type(r)
    r.findings = analyze(r)
    return r


def _parse_keyed_line(r: SysCheckReport, line: str) -> bool:
    """A ``Key: value`` line (region, console id, console type, country), in any of the
    report's languages. True if it was one of those and is now stored in ``r``."""
    key, _, val = line.partition(":")
    k, val = key.strip().lower(), val.strip()
    if k == "region" or k.startswith("région") or k.startswith("regione") \
            or k.startswith("región"):
        rm = re.match(r"([A-Za-z-]+)(?:\s*\((?:[^:]*):\s*([A-Z]+)\))?", val)
        if rm:
            r.region = rm.group(1)
            r.original_region = rm.group(2) or ""
        return True
    if "console" in k and "id" in k or "konsolen-id" in k:
        r.console_id = val
        return True
    if val.lower() in ("wii", "vwii", "unknown", "unbekannt", "inconnu", "sconosciuto",
                       "desconocido") and ("typ" in k or "tipo" in k or "type" in k):
        v = val.lower()
        r.console_type = v if v in ("wii", "vwii") else ""
        return True
    if "country" in k or "land" in k or "pays" in k or "paese" in k or "país" in k:
        r.country = val
        return True
    return False


def _refine_console_type(r: SysCheckReport) -> None:
    """What the report's own "console type" line can't say: a Wii mini (by its menu
    version), and vWii / Wii when the line is missing (older SysCheck versions)."""
    if r.sysmenu_version in MINI_SYSMENU:
        r.console_type = "mini"
    elif not r.console_type:
        if r.sysmenu_version in VWII_SYSMENU or any(i.vwii for i in r.ios.values()):
            r.console_type = "vwii"
        elif r.ios:
            r.console_type = "wii"


def _parse_ios(m: re.Match) -> IosInfo:
    i = IosInfo(int(m.group("slot")), vwii=bool(m.group("v")))
    if m.group("rev"):
        i.revision = int(m.group("rev"))
    base = m.group("base")
    if base:
        i.base = int(base.split("+")[0])
    i.info = (m.group("info") or "").strip()
    rest = (m.group("rest") or "").strip()
    rl = rest.lower()
    if "skipped" in rl or "übersprungen" in rl:
        i.skipped = True
    if "stub" in rl:
        i.stub = True
    if "bootmii" in rl:
        i.bootmii = True
    if "nandemu" in rl:
        i.nandemu = True
    if not (i.stub or i.bootmii or i.nandemu or i.skipped):
        i.patches = [p.strip() for p in rest.split(",") if p.strip()]
        if any(p.lower() in ("no patches", "keine patches", "sin parches", "aucun patch",
                             "nessuna patch") for p in i.patches):
            i.patches = []
    return i


def read_syscheck(path: Path) -> SysCheckReport:
    """Read and parse a SysCheck.csv file (any encoding SysCheck may produce)."""
    raw = Path(path).read_bytes()
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return parse_syscheck(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    return parse_syscheck(raw.decode("latin-1", "replace"))


def find_syscheck(layout: DriveLayout | Path) -> Optional[Path]:
    """Locate SysCheck.csv at the root of a drive (case-insensitive)."""
    root = DriveLayout.of(layout).root
    try:
        for p in root.iterdir():
            if p.is_file() and p.name.lower() == "syscheck.csv":
                return p
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------- analysis
def analyze(r: SysCheckReport) -> list[Finding]:
    """Turn a parsed report into plain-language findings with fix ids.

    Fix ids: ``update_43``, ``install_priiloader``, ``install_d2x_cios``,
    ``update_hbc``, ``install_hbc``, ``install_bootmii``, ``restore_ios``.
    """
    f: list[Finding] = []
    ctype = r.console_type
    if not r.ios:
        f.append(_note("error", "This doesn't look like a SysCheck report (no IOS list found).",
                         ""))
        return f
    korean_changed = r.korean_region_changed
    if r.sysmenu_version is None:
        f.append(_note("warning", "The System Menu version couldn't be read.", ""))
    elif ctype == "wii" and r.sysmenu_version < 512 and r.sysmenu_version not in VWII_SYSMENU \
            and not korean_changed:
        f.append(_note("warning", f"The Wii Menu is version {r.sysmenu or r.sysmenu_version}. "
                         "Updating to 4.3 is recommended (do it only after Priiloader and a "
                         "NAND backup).", Fix.UPDATE_43))
    if korean_changed:
        f.append(_note("error", "This is a Korean console that was changed to another region "
                         f"({r.region or r.sysmenu or 'unknown'}). Do NOT update the Wii Menu or "
                         "install a different Wii Menu on it: Wii Menu 4.2 and newer refuse to "
                         "start on such consoles (\"Error 003\"), which bricks them. ReWii won't "
                         "offer the 4.3 update for this console.", ""))
    elif r.original_region:
        f.append(_note("warning", f"This console was region-changed (originally "
                         f"{r.original_region}). Be careful with themes and system updates.",
                         ""))
    if not r.priiloader:
        f.append(_note("warning", "Priiloader isn't installed. It protects against many "
                         "bricks; install it.", Fix.INSTALL_PRIILOADER))
    if r.hbc_installed is False:
        f.append(_note("error", "The Homebrew Channel isn't installed.", Fix.INSTALL_HBC))
    elif r.hbc_version is not None and r.hbc_version < HBC_LATEST:
        f.append(_note("warning", "The Homebrew Channel is out of date ("
                         + ".".join(map(str, r.hbc_version)) + "); update to 1.1.2.", Fix.UPDATE_HBC))
    if r.hbc_ios is not None and r.hbc_ios != 58:
        f.append(_note("info", f"The Homebrew Channel runs on IOS{r.hbc_ios}, not IOS58; "
                         "USB 2.0 may not work in homebrew.", Fix.UPDATE_HBC))
    # cIOS
    missing, wrong = [], []
    for slot, base in EXPECTED_CIOS.items():
        i = r.ios.get(slot)
        if i is None or i.stub or not i.is_d2x:
            missing.append(slot)
            continue
        if i.base is not None and i.base != base:
            wrong.append(f"IOS{slot} uses base {i.base} (expected {base})")
        v = i.d2x_version
        if ctype != "mini" and (v is None or v < D2X_MIN_VERSION):
            wrong.append(f"IOS{slot} is {i.info or 'an old d2x'} (v{D2X_MIN_VERSION} expected)")
    if missing:
        f.append(_note("error" if len(missing) == len(EXPECTED_CIOS) else "warning",
                         "d2x cIOS is missing from slot(s) " + ", ".join(map(str, missing))
                         + ". USB loaders need it.", Fix.INSTALL_D2X_CIOS))
    if wrong:
        f.append(_note("warning", "cIOS should be reinstalled: " + "; ".join(wrong) + ".",
                         Fix.INSTALL_D2X_CIOS))
    for base in sorted(set(EXPECTED_CIOS.values())):
        b = r.ios.get(base)
        if b is not None and b.stub:
            f.append(_note("warning", f"IOS{base} is a stub, so the d2x cIOS built on it "
                             "can't be installed until it's restored.", Fix.RESTORE_IOS))
    if ctype == "wii" and 58 not in r.ios:
        f.append(_note("warning", "IOS58 is missing (needed for USB 2.0 in homebrew).",
                         "" if korean_changed else Fix.UPDATE_43))
    if ctype == "wii" and not r.bootmii_ios and not r.bootmii_boot2:
        f.append(_note("info", "BootMii isn't installed. It's needed for NAND backups and "
                         "unbricking.", Fix.INSTALL_BOOTMII))
    skipped = [s for s, i in r.ios.items() if i.skipped]
    if skipped:
        f.append(_note("info", f"{len(skipped)} IOS were skipped by SysCheck.", ""))
    if ctype == "mini":
        f.append(_note("info", "Wii mini detected: it has no SD slot and no BootMii, and "
                         "uses d2xl cIOS.", ""))
    if ctype == "vwii":
        f.append(_note("info", "vWii (Wii U) detected: BootMii and NAND restore aren't "
                         "available in Wii mode.", ""))
    return f


def profile_from_syscheck(report: SysCheckReport, current_type: str = "") -> tuple[str, str]:
    """``(console_type, region)`` in :mod:`config` terms (``""`` if unknown).

    The region follows the System Menu's region letter (what matters for
    themes/updates), falling back to the ``Region:`` line.

    SysCheck can't tell a Wii Family Edition from an original Wii (it reports
    both as "Wii"). Pass the type the user chose as ``current_type``: it is kept
    when the report describes the same platform, so "family" is never turned
    back into "wii".
    """
    region = _LETTER_TO_REGION.get(report.region_letter, "")
    if not region:
        region = {"NTSC-U": "NTSC-U", "PAL": "PAL", "NTSC-J": "NTSC-J",
                  "KOR": "NTSC-K"}.get(report.region, "")
    ctype = report.console_type
    if ctype and console.same_platform(ctype, current_type):
        ctype = current_type
    return ctype, region
