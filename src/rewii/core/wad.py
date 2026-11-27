"""WAD inspection, safety checks and staging for YAWMME.

A WAD is an installable Wii title package (certificate chain, ticket, TMD and
encrypted contents). This module reads WADs with libWiiPy, describes them in
plain terms (:func:`inspect_wad`), decides how risky installing one is for the
user's console (:func:`classify_risk`), and copies chosen WADs into the drive's
``wad/`` folder where YAWMME (YAWM ModMii Edition) finds them.
"""
from __future__ import annotations

import hashlib
import os
import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from collections.abc import Iterable

from libWiiPy.archive.u8 import IMETHeader
from libWiiPy.title import Title
from libWiiPy.title.crypto import decrypt_content

from .config import Settings
from .disc import REGION_BY_CHAR
from .layout import DriveLayout
from .progress import CancelToken, ProgressFn, Reporter

COPY_CHUNK = 4 * 1024 * 1024


class WadError(Exception):
    """The file is not a readable WAD."""


class Refused(Exception):
    """ReWii refuses an action because it could damage (brick) the console.

    The message is plain language and safe to show to the user as-is.
    """


# --------------------------------------------------------------------------- title ids
# High word of the title ID -> broad category (https://wiibrew.org/wiki/Title_database)
HIGH_SYSTEM = "00000001"        # boot2, System Menu, IOS, BC, MIOS (Wii)
HIGH_VWII_SYSTEM = "00000007"   # vWii System Menu and IOS
HIGH_DISC = "00010000"          # disc-based game data (saves etc.)
HIGH_CHANNEL = "00010001"       # downloaded channels: WiiWare, VC, homebrew forwarders
HIGH_SYSTEM_CHANNEL = "00010002"
HIGH_GAME_CHANNEL = "00010004"  # channels installed by a disc (e.g. Wii Fit Channel)
HIGH_DLC = "00010005"
HIGH_HIDDEN = "00010008"
HIGH_VWII_SYSTEM_CHANNEL = "00070002"
HIGH_VWII_HIDDEN = "00070008"

#: First letter of a 00010001 channel's 4-char ID used by Virtual Console titles.
VC_PREFIXES = {
    "C": "Commodore 64", "E": "Neo Geo / Arcade", "F": "NES", "J": "SNES",
    "L": "Master System", "M": "Mega Drive / Genesis", "N": "Nintendo 64",
    "P": "TurboGrafx-16", "Q": "TurboGrafx-CD",
}

# Title type labels
T_BOOT2 = "boot2"
T_SYSTEM_MENU = "System Menu"
T_IOS = "IOS"
T_BC = "BC"
T_MIOS = "MIOS"
T_SYSTEM = "System"
T_DISC = "Disc title"
T_CHANNEL = "Channel"
T_WIIWARE = "WiiWare"
T_VC = "Virtual Console"
T_SYSTEM_CHANNEL = "System Channel"
T_GAME_CHANNEL = "Game Channel"
T_DLC = "DLC"
T_HIDDEN = "Hidden Channel"
T_UNKNOWN = "Unknown"

SYSTEM_LEVEL_TYPES = {T_BOOT2, T_SYSTEM_MENU, T_IOS, T_BC, T_MIOS, T_SYSTEM}

#: TMD region field -> ReWii region name (config.REGIONS)
TMD_REGIONS = {0: "NTSC-J", 1: "NTSC-U", 2: "PAL", 3: "Region free", 4: "NTSC-K"}

#: System Menu version (v512 = 4.3J ...) -> region, from version & 0x1F.
#: Wii: 4.3J v512, 4.3U v513, 4.3E v514, 4.3K v518; vWii: v608/609/610 etc.
_SM_REGION_BY_LOW = {0: "NTSC-J", 1: "NTSC-U", 2: "PAL", 6: "NTSC-K"}

COMMON_KEY_NAMES = {0: "Normal", 1: "Korean", 2: "vWii"}

#: IOS slots a System Menu runs on (3.2: 30, 3.3K: 40, 3.4: 50, 3.5K: 52, 4.0/4.1: 60,
#: 4.2: 70, 4.3 and vWii: 80). A broken or stubbed copy of the one in use is an instant brick.
MENU_IOS_SLOTS = frozenset({30, 40, 50, 52, 60, 70, 80})
#: Title IDs of those IOS (lower-case hex; vWii WADs carry the same 00000001 IDs).
SYSTEM_MENU_IOS = frozenset(f"{HIGH_SYSTEM}{n:08x}" for n in MENU_IOS_SLOTS)

#: The only System Menu ReWii lets through: 4.3 for the console's platform and region.
SYSTEM_MENU_43 = {
    ("wii", "NTSC-J"): 512, ("wii", "NTSC-U"): 513, ("wii", "PAL"): 514, ("wii", "NTSC-K"): 518,
    ("vwii", "NTSC-J"): 608, ("vwii", "NTSC-U"): 609, ("vwii", "PAL"): 610,
}


def is_menu_ios(title_id: str) -> bool:
    """True if ``title_id`` is an IOS that some System Menu version runs on."""
    tid = title_id.lower()
    if len(tid) != 16 or tid[:8] not in (HIGH_SYSTEM, HIGH_VWII_SYSTEM):
        return False
    try:
        return int(tid[8:], 16) in MENU_IOS_SLOTS
    except ValueError:
        return False


def is_stub_ios_version(version: int) -> bool:
    """Nintendo's stub IOS (empty placeholders that replaced retired IOS, e.g. IOS60
    v6400, IOS70 v6912, IOS30 v2816) all have a version that is a multiple of 256;
    no working IOS does."""
    return version % 256 == 0


def system_menu_region(version: int) -> str | None:
    """Region of a System Menu title version (e.g. 513 -> "NTSC-U")."""
    return _SM_REGION_BY_LOW.get(version & 0x1F)


def title_ascii(title_id: str) -> str:
    """Low word of a title ID as 4 printable chars ("FBCE"), or its hex if not printable."""
    low = bytes.fromhex(title_id[8:16])
    s = low.decode("ascii", "replace")
    if all(c in string.ascii_letters + string.digits for c in s):
        return s
    return title_id[8:16].upper()


def title_type_of(title_id: str) -> str:
    """Classify a 16-hex-digit title ID."""
    tid = title_id.lower()
    high, low = tid[:8], tid[8:]
    if high in (HIGH_SYSTEM, HIGH_VWII_SYSTEM):
        n = int(low, 16)
        if n == 1:
            return T_BOOT2
        if n == 2:
            return T_SYSTEM_MENU
        if n == 0x100:
            return T_BC
        if n == 0x101:
            return T_MIOS
        if 3 <= n <= 0xFF:
            return T_IOS
        return T_SYSTEM
    if high == HIGH_DISC:
        return T_DISC
    if high == HIGH_CHANNEL:
        a = title_ascii(tid)
        if a[:1] == "W":
            return T_WIIWARE
        if a[:1] in VC_PREFIXES and len(a) == 4:
            return T_VC
        return T_CHANNEL
    if high in (HIGH_SYSTEM_CHANNEL, HIGH_VWII_SYSTEM_CHANNEL):
        return T_SYSTEM_CHANNEL
    if high == HIGH_GAME_CHANNEL:
        return T_GAME_CHANNEL
    if high == HIGH_DLC:
        return T_DLC
    if high in (HIGH_HIDDEN, HIGH_VWII_HIDDEN):
        return T_HIDDEN
    return T_UNKNOWN


def pretty_title_id(title_id: str) -> str:
    """"0001000146424345" -> "00010001-46424345"."""
    return f"{title_id[:8].upper()}-{title_id[8:].upper()}"


# --------------------------------------------------------------------------- info
@dataclass
class WadInfo:
    """Everything ReWii shows about a WAD."""

    path: Optional[Path]
    title_id: str                 # 16 lowercase hex digits
    title_ascii: str              # 4-char ID ("FBCE") or hex low word
    title_type: str               # one of the T_* labels
    name: str                     # channel name from the banner, or a generated name
    region: str                   # NTSC-U/PAL/NTSC-J/NTSC-K/Region free/World/Unknown
    id_region: str                # region implied by the 4th char of the ID (channels)
    version: int
    required_ios: int             # IOS the title runs on (0 for IOS/boot2 themselves)
    file_size: int
    installed_size: int           # decrypted content bytes (shared contents excluded)
    num_contents: int
    common_key_index: int         # 0 normal, 1 Korean, 2 vWii (other values act as 0)
    platform: str                 # "wii" | "vwii"
    signed: bool                  # valid Nintendo signatures on TMD and ticket
    tmd_fakesigned: bool
    ticket_fakesigned: bool
    hashes_ok: Optional[bool]     # None when the check was skipped
    bad_contents: list[int] = field(default_factory=list)
    wad_type: str = "Is"          # "Is" normal, "ib" boot2

    @property
    def fakesigned(self) -> bool:
        return self.tmd_fakesigned and self.ticket_fakesigned

    @property
    def signature_status(self) -> str:
        """"signed", "fakesigned", "partly fakesigned" or "invalid"."""
        if self.signed:
            return "signed"
        if self.fakesigned:
            return "fakesigned"
        if self.tmd_fakesigned or self.ticket_fakesigned:
            return "partly fakesigned"
        return "invalid"

    @property
    def pretty_id(self) -> str:
        return pretty_title_id(self.title_id)

    @property
    def common_key_name(self) -> str:
        return COMMON_KEY_NAMES.get(self.common_key_index, f"Unknown ({self.common_key_index})")

    @property
    def is_system(self) -> bool:
        return self.title_type in SYSTEM_LEVEL_TYPES

    @property
    def ios_slot(self) -> Optional[int]:
        """IOS number when this WAD is an IOS (e.g. 58)."""
        return int(self.title_id[8:], 16) if self.title_type == T_IOS else None


def _channel_name(title: Title) -> str:
    """Channel name from the IMET banner header in content index 0, preferring English."""
    try:
        idx = next(i for i, r in enumerate(title.content.content_records) if r.index == 0)
        data = _decrypt(title, idx)
    except Exception:
        return ""
    pos = data.find(b"IMET", 0, 0x400)
    if pos < 0x40:
        return ""
    h = IMETHeader()
    try:
        h.load(data[pos - 0x40:pos - 0x40 + 0x600])
    except Exception:
        return ""
    names = [n.strip() for n in h.channel_names]
    order = [1, 3, 2, 4, 5, 6, 0, 7, 8, 9]   # English, then other languages
    for i in order:
        if i < len(names) and names[i]:
            return names[i]
    return ""


def _decrypt(title: Title, idx: int) -> bytes:
    rec = title.content.content_records[idx]
    enc = title.content.get_enc_content_by_index(idx)
    return decrypt_content(enc, title.ticket.get_title_key(), rec.index, rec.content_size)


def raw_fakesigned(blob: bytes) -> bool:
    """Trucha fakesign check on a raw TMD or ticket: zeroed RSA signature and a
    body SHA-1 starting with 00 (checked on the raw bytes, not a re-serialisation)."""
    if len(blob) < 0x140 + 4 or blob[0:4] != b"\x00\x01\x00\x01":
        return False
    return blob[4:0x104] == bytes(0x100) and hashlib.sha1(blob[0x140:]).digest()[0] == 0


def _is_signed(title: Title) -> bool:
    try:
        return bool(title.get_is_signed())
    except Exception:
        return False


def _default_name(tid: str, ttype: str) -> str:
    if ttype == T_SYSTEM_MENU:
        return "System Menu"
    if ttype == T_IOS:
        return f"IOS{int(tid[8:], 16)}"
    if ttype == T_BOOT2:
        return "boot2"
    return title_ascii(tid)


def load_title(data: bytes) -> Title:
    """Parse WAD bytes into a libWiiPy Title, raising :class:`WadError` if invalid."""
    t = Title()
    try:
        t.load_wad(data)
    except Exception as e:  # libWiiPy raises TypeError/ValueError/struct errors
        raise WadError(f"Not a valid WAD file ({e})") from e
    return t


def info_from_title(title: Title, *, path: Optional[Path] = None, file_size: int = 0,
                    verify_hashes: bool = True, reporter: Reporter | None = None) -> WadInfo:
    """Build a :class:`WadInfo` from an already-loaded Title."""
    tmd, tik = title.tmd, title.ticket
    tid = tmd.title_id.lower()
    ttype = title_type_of(tid)
    ascii_id = title_ascii(tid)
    vwii = tid[:4] == "0007" or tid[:8] == HIGH_VWII_SYSTEM or bool(tmd.vwii)
    vwii = vwii or tik.common_key_index == 2
    if ttype == T_SYSTEM_MENU:
        region = system_menu_region(tmd.title_version) or "Unknown"
    elif ttype in (T_IOS, T_BOOT2, T_BC, T_MIOS, T_SYSTEM):
        region = "World"
    else:
        region = TMD_REGIONS.get(tmd.region, "Unknown")
    id_region = REGION_BY_CHAR.get(ascii_id[3:4].upper(), "Unknown") if len(ascii_id) == 4 else "Unknown"

    hashes_ok: Optional[bool] = None
    bad: list[int] = []
    if verify_hashes:
        recs = title.content.content_records
        n_loaded = len(title.content.content_list)
        if reporter:
            reporter.set_total(sum(r.content_size for r in recs), "Checking contents")
        for i, rec in enumerate(recs):
            if i >= n_loaded or not title.content.content_list[i]:
                bad.append(rec.index)
                continue
            try:
                dec = _decrypt(title, i)
                ok = hashlib.sha1(dec).hexdigest() == rec.content_hash.decode().lower()
            except Exception:
                ok = False
            if not ok:
                bad.append(rec.index)
            if reporter:
                reporter.advance(rec.content_size)
        hashes_ok = not bad

    name = ""
    if ttype not in (T_IOS, T_BOOT2, T_BC, T_MIOS, T_SYSTEM_MENU):
        name = _channel_name(title)
    return WadInfo(
        path=path,
        title_id=tid,
        title_ascii=ascii_id,
        title_type=ttype,
        name=name or _default_name(tid, ttype),
        region=region,
        id_region=id_region,
        version=tmd.title_version,
        required_ios=0 if ttype in (T_IOS, T_BOOT2) else tmd.ios_version,
        file_size=file_size,
        installed_size=tmd.get_content_size(),
        num_contents=tmd.num_contents,
        common_key_index=tik.common_key_index if tik.common_key_index in (0, 1, 2) else 0,
        platform="vwii" if vwii else "wii",
        signed=_is_signed(title),
        tmd_fakesigned=raw_fakesigned(title.wad.get_tmd_data()) or tmd.get_is_fakesigned(),
        ticket_fakesigned=raw_fakesigned(title.wad.get_ticket_data()) or tik.get_is_fakesigned(),
        hashes_ok=hashes_ok,
        bad_contents=bad,
        wad_type=title.wad.wad_type,
    )


def inspect_wad(path: Path | str, *, verify_hashes: bool = True,
                progress: ProgressFn | None = None, cancel: CancelToken | None = None) -> WadInfo:
    """Read a WAD file and describe it.

    ``verify_hashes`` decrypts every content and compares its SHA-1 with the TMD
    (slower; set False for quick library listings).
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise WadError(f"Could not read {path.name}: {e.strerror or e}") from e
    if len(data) < 0x40 or data[:8] not in (b"\x00\x00\x00\x20Is\x00\x00", b"\x00\x00\x00\x20ib\x00\x00"):
        raise WadError(f"{path.name} is not a WAD file")
    title = load_title(data)
    rep = Reporter(progress, cancel) if (progress or cancel) else None
    return info_from_title(title, path=path, file_size=len(data), verify_hashes=verify_hashes,
                           reporter=rep)


# --------------------------------------------------------------------------- risk
@dataclass
class Risk:
    """One finding about installing a WAD. ``level`` is "error" (blocked), "warning" or "info"."""

    level: str
    code: str
    message: str

    @property
    def blocks(self) -> bool:
        return self.level == "error"


def _console_label(ct: str) -> str:
    return {"wii": "Wii", "vwii": "Wii U (vWii)", "mini": "Wii mini",
            "family": "Wii Family Edition"}.get(ct, ct or "unknown console")


def classify_risk(info: WadInfo, settings: Settings) -> list[Risk]:
    """Decide how safe installing ``info`` is on the console in ``settings``.

    Errors (``Risk.blocks``) mean ReWii must not stage or install it: system
    files for the wrong console type or region, boot2, damaged WADs, or vWii
    titles on a Wii. Warnings explain what could go wrong; infos are notes.
    """
    risks: list[Risk] = []
    ct = settings.console_type
    reg = settings.console_region
    wii_like = ct in ("wii", "family")
    label = _console_label(ct)

    def add(level: str, code: str, msg: str) -> None:
        risks.append(Risk(level, code, msg))

    if info.hashes_ok is False:
        add("error", "corrupt", "This WAD is damaged (its contents don't match their checksums). "
            "Installing it could break the title or the console. Download it again.")

    t = info.title_type
    if t == T_BOOT2 or info.wad_type == "ib":
        add("error", "boot2", "This WAD contains boot2, the Wii's lowest-level boot code. "
            "Installing it is extremely dangerous and ReWii will never do it.")
        return risks

    if info.is_system:
        what = {"System Menu": "the Wii Menu (System Menu)", "IOS": f"system software ({info.name})"}.get(
            t, "a core system title")
        if not ct or not reg:
            add("error", "profile_unset", f"This WAD replaces {what}. Tell ReWii your console type and "
                "region first (Settings) so it can check it is the right one.")
            return risks
        if ct == "mini" and t in (T_SYSTEM_MENU, T_IOS, T_BC, T_MIOS, T_SYSTEM):
            add("error", "mini_system", f"This WAD replaces {what}. The Wii mini needs its own special "
                "versions; installing this one will likely brick it.")
        elif info.platform == "vwii" and wii_like:
            add("error", "platform_mismatch", f"This WAD is {what} for the Wii U's vWii mode. "
                f"Installing it on a {label} will brick it.")
        elif info.platform == "wii" and ct == "vwii":
            add("error", "platform_mismatch", f"This WAD is {what} for an original Wii. "
                "Installing it on a Wii U's vWii mode will brick it.")
        if t == T_SYSTEM_MENU:
            sm_reg = system_menu_region(info.version)
            if sm_reg is None:
                add("error", "region_unknown", "ReWii can't tell which region this Wii Menu is for, "
                    "so it won't install it.")
            elif sm_reg != reg:
                add("error", "region_mismatch", f"This Wii Menu is for {sm_reg} consoles but yours is "
                    f"{reg}. Installing it will brick your console.")
            else:
                plat = "vwii" if ct == "vwii" else "wii"
                want = SYSTEM_MENU_43.get((plat, reg))
                if want is None or info.version != want:
                    add("error", "menu_version", "ReWii only installs Wii Menu 4.3 for your console "
                        f"({'v' + str(want) if want else 'not available for this region'}); this "
                        f"WAD is version v{info.version}. Installing an older or different Wii Menu "
                        "can brick the console.")
            if not info.signed:
                add("error", "modified_menu", "This Wii Menu is modified or not signed by Nintendo. "
                    "Installing a changed Wii Menu can brick the console; use themes "
                    "(csm-installer) to change its look instead.")
            add("warning", "system_menu", "Replacing the Wii Menu is risky. Make sure Priiloader "
                "(and BootMii, on a Wii) is installed before you continue.")
        if t == T_IOS:
            if is_menu_ios(info.title_id):
                if is_stub_ios_version(info.version):
                    add("error", "stub_menu_ios", f"{info.name} v{info.version} is an empty placeholder "
                        "(a \"stub\"). If your Wii Menu runs on this IOS, installing it bricks the "
                        "console.")
                elif not info.signed:
                    add("error", "modified_menu_ios", f"{info.name} is the IOS your Wii Menu runs on, "
                        "and this copy is modified or not properly signed. Installing it can brick "
                        "the console.")
            add("warning", "ios", f"This WAD replaces {info.name}, part of the console's system "
                "software. Only install IOS from Nintendo's servers or the guide.")
        if t in (T_BC, T_MIOS, T_SYSTEM):
            add("warning", "system_title", "This is a core system title. Only install it if a guide "
                "tells you to.")
    else:
        if info.common_key_index == 2 and ct and ct != "vwii":
            add("error", "vwii_key", f"This WAD is encrypted for the Wii U's vWii mode and can't be "
                f"installed on a {label}.")
        elif info.platform == "vwii" and ct and ct != "vwii":
            add("error", "platform_mismatch", f"This is a vWii (Wii U) title. It can't be installed on "
                f"a {label}.")
        if t == T_SYSTEM_CHANNEL and ct == "vwii" and info.platform == "wii":
            add("warning", "wii_system_channel_on_vwii", "This is an original-Wii system channel. vWii "
                "has its own versions; this one may not work or may cause problems.")
        if t == T_HIDDEN:
            add("warning", "hidden", "This is a hidden system title. Only install it if a guide tells "
                "you to.")
        if t == T_SYSTEM_CHANNEL and ct and ct != "vwii":
            add("info", "system_channel", "This is a system channel (for example the Shop or Photo "
                "channel). Make sure it matches your console region.")
        if reg and info.region in ("NTSC-U", "PAL", "NTSC-J", "NTSC-K") and info.region != reg:
            add("warning", "region_mismatch", f"This title is for {info.region} consoles and yours is "
                f"{reg}. It may not appear or may not start.")
        if info.common_key_index == 1 and reg and reg != "NTSC-K":
            add("warning", "korean_key", "This WAD uses the Korean encryption key; it may fail to "
                "install on non-Korean consoles.")

    if not info.signed:
        if info.fakesigned:
            add("info", "fakesigned", "This WAD is fakesigned (homebrew-made or modified). YAWMME "
                "can install it; that is normal for forwarders and patched titles.")
        else:
            add("warning", "bad_signature", "This WAD isn't properly signed or fakesigned. YAWMME may "
                "refuse to install it, or it may not run.")
    return risks


def is_blocked(risks: Iterable[Risk]) -> bool:
    """True if any risk is an error."""
    return any(r.blocks for r in risks)


def ensure_allowed(info: WadInfo, settings: Settings) -> list[Risk]:
    """Raise :class:`Refused` with the first blocking reason, else return the risks.

    A WAD whose contents haven't been checked yet (``info.hashes_ok is None``,
    e.g. from a quick :func:`scan_wads`) is verified here first - its file is read
    again and ``info`` is updated in place. If there is no file to check, it is
    refused: nothing unverified gets through.
    """
    if info.hashes_ok is None:
        if info.path is None:
            raise Refused("This WAD's contents haven't been checked, so ReWii won't use it.")
        try:
            full = inspect_wad(info.path, verify_hashes=True)
        except WadError as e:
            raise Refused(str(e)) from e
        if full.title_id != info.title_id or full.version != info.version:
            raise Refused("This WAD changed while ReWii was checking it. Try again.")
        info.hashes_ok, info.bad_contents = full.hashes_ok, full.bad_contents
        info.signed = full.signed
        info.tmd_fakesigned, info.ticket_fakesigned = full.tmd_fakesigned, full.ticket_fakesigned
        info.wad_type, info.platform = full.wad_type, full.platform
    risks = classify_risk(info, settings)
    for r in risks:
        if r.blocks:
            raise Refused(r.message)
    return risks


# --------------------------------------------------------------------------- library
@dataclass
class ScanResult:
    wads: list[WadInfo] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)


def scan_wads(folder: Path | str, *, recursive: bool = True, verify_hashes: bool = False,
              cancel: CancelToken | None = None) -> ScanResult:
    """Find and inspect every ``*.wad`` under ``folder`` (quick mode by default)."""
    folder = Path(folder)
    res = ScanResult()
    if not folder.is_dir():
        return res
    it = folder.rglob("*") if recursive else folder.iterdir()
    paths = sorted(p for p in it if p.is_file() and p.suffix.lower() == ".wad"
                   and not p.name.startswith("._"))
    for p in paths:
        if cancel is not None:
            cancel.check()
        try:
            res.wads.append(inspect_wad(p, verify_hashes=verify_hashes))
        except WadError as e:
            res.errors.append((p, str(e)))
    return res


_FAT_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def staged_name(path: Path) -> str:
    """A FAT32-safe file name for a WAD in ``wad/``."""
    stem = _FAT_BAD.sub("", Path(path).stem).strip().rstrip(".") or "title"
    stem = re.sub(r"\s+", " ", stem)[:80].rstrip(" .") or "title"
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?", stem):
        stem = "_" + stem           # Windows device names can't be file names
    return f"{stem}.wad"


def _same_file(a: Path, b: Path) -> bool:
    if a.stat().st_size != b.stat().st_size:
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(COPY_CHUNK), fb.read(COPY_CHUNK)
            if x != y:
                return False
            if not x:
                return True


def stage_wads(layout: DriveLayout, paths: Iterable[Path | str], progress: ProgressFn | None = None,
               cancel: CancelToken | None = None, *, settings: Settings | None = None) -> list[Path]:
    """Copy WADs into the drive's ``wad/`` folder (YAWMME opens it automatically).

    If ``settings`` is given every WAD is checked with :func:`classify_risk`
    first and :class:`Refused` is raised (nothing copied) if any is blocked.
    Identical files already staged are skipped; name clashes get " (2)" etc.
    Returns the staged paths in input order.
    """
    srcs = [Path(p) for p in paths]
    for s in srcs:
        if not s.is_file():
            raise WadError(f"{s.name} not found")
        if settings is not None:
            info = inspect_wad(s, verify_hashes=True)
            try:
                ensure_allowed(info, settings)
            except Refused as e:
                raise Refused(f"{s.name}: {e}") from e
    rep = Reporter(progress, cancel)
    rep.set_total(sum(s.stat().st_size for s in srcs), "Copying WADs")
    layout.wad.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for s in srcs:
        rep.check()
        name = staged_name(s)
        dest = layout.wad / name
        n = 2
        while dest.exists():
            if _same_file(s, dest):
                break
            dest = layout.wad / f"{Path(name).stem} ({n}).wad"
            n += 1
        if dest.exists():
            rep.advance(s.stat().st_size, f"{s.name} already staged")
            out.append(dest)
            continue
        tmp = dest.with_name(dest.name + ".part")
        try:
            with open(s, "rb") as fi, open(tmp, "wb") as fo:
                while True:
                    b = fi.read(COPY_CHUNK)
                    if not b:
                        break
                    fo.write(b)
                    rep.advance(len(b), f"Copying {s.name}")
                fo.flush()
                os.fsync(fo.fileno())
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        out.append(dest)
    return out


def list_staged(layout: DriveLayout) -> ScanResult:
    """WADs currently in the drive's ``wad/`` folder (quick inspection)."""
    return scan_wads(layout.wad, recursive=False)


def unstage(layout: DriveLayout, items: Iterable[Path | str]) -> list[Path]:
    """Delete staged WADs (by name or path) from ``wad/``. Returns what was removed.

    Paths outside ``wad/`` are refused so this can't delete anything else.
    """
    base = layout.wad.resolve()
    removed: list[Path] = []
    for it in items:
        p = Path(it)
        if not p.is_absolute():
            p = layout.wad / p
        rp = p.resolve()
        if rp.parent != base or rp.suffix.lower() != ".wad":
            raise ValueError(f"{p} is not a staged WAD")
        if rp.exists():
            rp.unlink()
            removed.append(rp)
    return removed


# --------------------------------------------------------------------------- pack / unpack
def unpack_wad(wad_path: Path | str, out_dir: Path | str, *, skip_hash: bool = False) -> Path:
    """Unpack a WAD into ``out_dir``.

    Writes ``<tid>.tik``, ``<tid>.tmd``, ``<tid>.cert``, ``<tid>.footer`` (only
    if the WAD has one) and one decrypted ``<content id, 8 hex>.app`` per content.
    """
    wad_path, out = Path(wad_path), Path(out_dir)
    title = load_title(wad_path.read_bytes())
    out.mkdir(parents=True, exist_ok=True)
    tid = title.tmd.title_id.lower()
    (out / f"{tid}.tik").write_bytes(title.wad.get_ticket_data())
    (out / f"{tid}.tmd").write_bytes(title.wad.get_tmd_data())
    (out / f"{tid}.cert").write_bytes(title.wad.get_cert_data())
    meta = title.wad.get_meta_data()
    if meta:
        (out / f"{tid}.footer").write_bytes(meta)
    for i, rec in enumerate(title.content.content_records):
        dec = _decrypt(title, i)
        if not skip_hash and hashlib.sha1(dec).hexdigest() != rec.content_hash.decode().lower():
            raise WadError(f"Content {rec.content_id:08x} is damaged (checksum mismatch)")
        (out / f"{rec.content_id:08x}.app").write_bytes(dec)
    return out


def _one(folder: Path, suffix: str) -> Path:
    found = [p for p in folder.iterdir() if p.suffix.lower() == suffix]
    if len(found) != 1:
        raise WadError(f"Expected exactly one {suffix} file in {folder.name}, found {len(found)}")
    return found[0]


def pack_wad(folder: Path | str, dest: Path | str, *, fakesign: Optional[bool] = None) -> Path:
    """Pack an unpacked WAD folder (see :func:`unpack_wad`) back into a WAD.

    Changed ``.app`` files are re-encrypted and their size/hash updated in the
    TMD. ``fakesign``: None = only if something changed, True = always, False = never.
    """
    folder, dest = Path(folder), Path(dest)
    title = Title()
    title.load_tmd(_one(folder, ".tmd").read_bytes())
    title.load_ticket(_one(folder, ".tik").read_bytes())
    from libWiiPy.title.cert import CertificateChain
    chain = CertificateChain()
    try:
        chain.load(_one(folder, ".cert").read_bytes())
    except WadError:
        raise
    except Exception as e:
        raise WadError(f"Certificate chain is invalid ({e})") from e
    title.cert_chain = chain
    title.load_content_records()
    files = {p.name.lower(): p for p in folder.iterdir() if p.suffix.lower() == ".app"}
    changed = False
    for i, rec in enumerate(list(title.content.content_records)):
        p = files.get(f"{rec.content_id:08x}.app")
        if p is None:
            raise WadError(f"Missing content {rec.content_id:08x}.app")
        dec = p.read_bytes()
        if hashlib.sha1(dec).hexdigest() == rec.content_hash.decode().lower():
            title.content.load_content(dec, i, title.ticket.get_title_key())
        else:
            title.set_content(dec, i)
            changed = True
    footers = [p for p in folder.iterdir() if p.suffix.lower() == ".footer"]
    if footers:
        title.wad.set_meta_data(footers[0].read_bytes())
    if fakesign or (fakesign is None and changed):
        title.fakesign()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(title.dump_wad())
    os.replace(tmp, dest)
    return dest

