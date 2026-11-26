"""Guarded downloads from Nintendo's update servers (NUS).

Builds installable WADs for the few system titles the wii.hacks.guide asks for:

* the four base IOS the d2x cIOS installer needs (Wii and vWii versions),
* the Wii Menu 4.3 update (System Menu + IOS80 + IOS58, original Wii only),
* the decrypted Wii Menu "base theme" ``.app`` used to build themes.

Every high-level function checks the console profile in :class:`Settings` and
raises :class:`Refused` if the console type/region is unset or wrong, because
the wrong System Menu or IOS bricks consoles.

The HTTP calls are reimplemented with :mod:`rewii.core.net` (libWiiPy's own
downloader uses ``requests`` without progress or cancellation). Endpoints and
the ``wii libnup/1.0`` User-Agent match libWiiPy's ``title/nus.py``.
Downloads are verified end-to-end: Nintendo's RSA signatures on the TMD and
ticket must check out and every decrypted content must match its SHA-1.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from libWiiPy.title import Title
from libWiiPy.title.cert import CertificateChain
from libWiiPy.title.crypto import decrypt_content, encrypt_title_key

from . import net, wad
from .config import Settings
from .layout import DriveLayout
from .progress import CancelToken, ProgressFn, Reporter
from .wad import Refused, ensure_allowed, info_from_title

NUS_BASE = "http://nus.cdn.shop.wii.com/ccs/download/"
NUS_USER_AGENT = "wii libnup/1.0"

TMD_HEADER = 0x1E4
TMD_RECORD = 36
TICKET_SIZE = 0x2A4
CERT_RSA2048 = 0x300   # CP / XS certificates (signed by the 4096-bit CA)
CERT_RSA4096 = 0x400   # CA certificate (signed by Root)
MAX_META_BYTES = 1024 * 1024       # a TMD or cetk is a few KB
MAX_CONTENT_BYTES = 512 * 1024 ** 2


class NusError(Exception):
    """A download from Nintendo's servers failed or didn't verify."""


# --------------------------------------------------------------------------- catalog
@dataclass(frozen=True)
class CatalogEntry:
    """One known-good download."""

    key: str
    title_id: str            # 16 hex digits, lowercase
    version: int
    platform: str            # "wii" | "vwii"
    region: Optional[str]    # None = all regions
    filename: str            # name the consumer expects
    description: str
    wii_common_key: bool = False   # re-encrypt the title key with the Wii common key (vWii IOS)


def _ios(n: int, ver: int, platform: str, purpose: str, *, filename: str | None = None,
         rekey: bool = False) -> CatalogEntry:
    high = "00000001" if platform == "wii" else "00000007"
    return CatalogEntry(
        key=f"{platform}-ios{n}-v{ver}", title_id=f"{high}{n:08x}", version=ver, platform=platform,
        region=None, filename=filename or f"IOS{n}-64-v{ver}.wad", description=purpose,
        wii_common_key=rekey)


#: Base IOS for the d2x cIOS installer, named as wii.hacks.guide docs/cios.md requires
#: ("IOS38-64-v4123.wad" ...). vWii versions are re-encrypted with the Wii common key,
#: as the guide does with NUSGet's "Re-encrypt title using the Wii Common Key".
CIOS_BASE: dict[str, list[CatalogEntry]] = {
    "wii": [
        _ios(38, 4123, "wii", "d2x cIOS base (slot 248)"),
        _ios(56, 5661, "wii", "d2x cIOS base (slot 249)"),
        _ios(57, 5918, "wii", "d2x cIOS base (slot 250)"),
        _ios(58, 6175, "wii", "d2x cIOS base (slot 251)"),
    ],
    "vwii": [
        _ios(38, 4380, "vwii", "d2x cIOS base (slot 248)", rekey=True),
        _ios(56, 5918, "vwii", "d2x cIOS base (slot 249)", rekey=True),
        _ios(57, 6175, "vwii", "d2x cIOS base (slot 250)", rekey=True),
        _ios(58, 6432, "vwii", "d2x cIOS base (slot 251)", rekey=True),
    ],
}

#: Wii Menu 4.3 per region (wii.hacks.guide docs/update.md; versions from NUSGet's database).
SYSTEM_MENU_43 = {region: v for (plat, region), v in wad.SYSTEM_MENU_43.items() if plat == "wii"}
_REGION_LETTER = {"NTSC-U": "U", "PAL": "E", "NTSC-J": "J", "NTSC-K": "K"}

#: Latest IOS80 / IOS58 on NUS (NUSGet wii-database.json: IOS80 6943/6944, IOS58 6175/6176).
UPDATE_43_IOS = [
    _ios(80, 6944, "wii", "Wii Menu 4.3 IOS"),
    _ios(58, 6176, "wii", "IOS58 (USB 2.0, used by the Homebrew Channel)"),
]


def system_menu_43(region: str) -> CatalogEntry:
    """The Wii Menu 4.3 entry for a region."""
    ver = SYSTEM_MENU_43[region]
    return CatalogEntry(key=f"wii-sm43{_REGION_LETTER[region]}", title_id="0000000100000002",
                        version=ver, platform="wii", region=region,
                        filename=f"SystemMenu-4.3{_REGION_LETTER[region]}-v{ver}.wad",
                        description=f"Wii Menu 4.3{_REGION_LETTER[region]}")


@dataclass(frozen=True)
class ThemeBase:
    title_id: str
    version: int
    content_id: int

    @property
    def filename(self) -> str:
        return f"{self.content_id:08X}.app"


#: Base theme .app per (platform, region). Wii: the largest content of System Menu 4.3
#: (verified against the NUS TMDs); vWii: v608/609/610 contents 0000001C/1F/22
#: (wii.hacks.guide docs/themes.md, also verified against the NUS TMDs).
THEME_BASE: dict[tuple[str, str], ThemeBase] = {
    ("wii", "NTSC-J"): ThemeBase("0000000100000002", 512, 0x94),
    ("wii", "NTSC-U"): ThemeBase("0000000100000002", 513, 0x97),
    ("wii", "PAL"): ThemeBase("0000000100000002", 514, 0x9A),
    ("wii", "NTSC-K"): ThemeBase("0000000100000002", 518, 0x9D),
    ("vwii", "NTSC-J"): ThemeBase("0000000700000002", 608, 0x1C),
    ("vwii", "NTSC-U"): ThemeBase("0000000700000002", 609, 0x1F),
    ("vwii", "PAL"): ThemeBase("0000000700000002", 610, 0x22),
}


# --------------------------------------------------------------------------- guards
def _platform_of(settings: Settings) -> str:
    return {"wii": "wii", "family": "wii", "vwii": "vwii", "mini": "mini"}.get(settings.console_type, "")


def require_profile(settings: Settings, allowed: tuple[str, ...], what: str) -> str:
    """Raise :class:`Refused` unless the console profile is set and its type is in ``allowed``.

    Returns the platform ("wii" or "vwii").
    """
    if not settings.console_type or not settings.console_region:
        raise Refused(f"Before ReWii can prepare {what}, tell it your console type and region "
                      "(Settings). Downloading the wrong system files can brick a console.")
    if settings.console_type not in allowed:
        names = {"wii": "Wii", "family": "Wii Family Edition", "vwii": "Wii U (vWii)", "mini": "Wii mini"}
        raise Refused(f"{what[:1].upper()}{what[1:]} isn't available for the "
                      f"{names.get(settings.console_type, settings.console_type)}.")
    return _platform_of(settings)


# --------------------------------------------------------------------------- HTTP
def _url(title_id: str, name: str) -> str:
    return f"{NUS_BASE}{title_id.lower()}/{name}"


def _fetch(url: str, rep: Reporter | None = None, ok404: bool = False, *,
           max_bytes: int = MAX_META_BYTES) -> Optional[bytes]:
    """GET a NUS file with the NUS User-Agent, streaming with progress/cancel.

    Raises :class:`NusError` if the server sends more than ``max_bytes``.
    """
    buf = bytearray()
    last_err: Exception | None = None
    what = f"{url.rsplit('/', 2)[-2]}/{url.rsplit('/', 1)[-1]}"
    for _attempt in range(2):
        buf.clear()
        try:
            with net.client().stream("GET", url, headers={"User-Agent": NUS_USER_AGENT}) as r:
                if r.status_code == 404 and ok404:
                    return None
                if r.status_code != 200:
                    raise NusError(f"Nintendo's server returned HTTP {r.status_code} for {what}")
                for chunk in r.iter_bytes(256 * 1024):
                    buf += chunk
                    if len(buf) > max_bytes:
                        raise NusError(f"Nintendo's server sent too much data for {what}; "
                                       "the download is not genuine")
                    if rep is not None:
                        rep.advance(len(chunk))
            return bytes(buf)
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            last_err = e
            if rep is not None:
                rep.update(max(0, rep.done - len(buf)))
    raise NusError(f"Could not reach Nintendo's update server: {last_err}")


def download_tmd(title_id: str, version: Optional[int] = None) -> bytes:
    """Raw TMD (with its certificates appended, as NUS serves it)."""
    name = "tmd" if version is None else f"tmd.{version}"
    data = _fetch(_url(title_id, name), ok404=True)
    if data is None:
        raise NusError(f"Title {title_id} v{version} does not exist on Nintendo's server")
    return data


def download_cetk(title_id: str) -> Optional[bytes]:
    """Raw ``cetk`` (ticket + certificates), or None if the title has no public ticket."""
    return _fetch(_url(title_id, "cetk"), ok404=True)


def tmd_length(raw_tmd: bytes) -> int:
    n = int.from_bytes(raw_tmd[0x1DE:0x1E0], "big")
    return TMD_HEADER + TMD_RECORD * n


def build_cert_chain(raw_tmd: bytes, cetk: bytes) -> bytes:
    """Assemble the WAD certificate chain (CA, CP, XS) from what NUS serves.

    NUS appends CP + CA certs after the TMD and XS + CA certs after the ticket.
    """
    tl = tmd_length(raw_tmd)
    cp = raw_tmd[tl:tl + CERT_RSA2048]
    xs = cetk[TICKET_SIZE:TICKET_SIZE + CERT_RSA2048]
    ca = cetk[TICKET_SIZE + CERT_RSA2048:TICKET_SIZE + CERT_RSA2048 + CERT_RSA4096]
    if len(cp) != CERT_RSA2048 or len(xs) != CERT_RSA2048 or len(ca) != CERT_RSA4096:
        raise NusError("Nintendo's server sent incomplete certificates")
    return ca + cp + xs


# --------------------------------------------------------------------------- titles
def expected_title_id(title_id: str) -> str:
    """The title ID a genuine TMD/ticket carries for a NUS download path.

    vWii system titles live under ``00000007xxxxxxxx`` on the server, but their
    TMD and ticket say ``00000001xxxxxxxx`` (with the TMD's vWii flag set).
    """
    tid = title_id.lower()
    return HIGH_WII_SYSTEM + tid[8:] if tid[:8] == HIGH_VWII_SYSTEM else tid


HIGH_WII_SYSTEM = wad.HIGH_SYSTEM
HIGH_VWII_SYSTEM = wad.HIGH_VWII_SYSTEM


def _padded(size: int) -> int:
    return (size + 15) // 16 * 16


def load_verified(title_id: str, version: Optional[int]) -> Title:
    """Download a title's TMD, ticket and certificates and prove they are genuine.

    Used by every download: the TMD and ticket must parse, carry exactly the
    title ID (and version, when one is asked for) that was requested, and have
    valid Nintendo signatures. Anything else raises :class:`NusError`, so a
    captive portal page or a different (even genuine) title can't be mistaken
    for the requested one. The returned Title has no contents yet.
    """
    if not re.fullmatch(r"[0-9a-fA-F]{16}", title_id or ""):
        raise NusError(f"{title_id!r} is not a title ID")
    raw_tmd = download_tmd(title_id, version)
    cetk = download_cetk(title_id)
    if cetk is None:
        raise NusError(f"Title {title_id} has no public ticket, so it can't be packed into a WAD")
    t = Title()
    try:
        t.load_tmd(raw_tmd[:tmd_length(raw_tmd)])
        t.load_ticket(cetk[:TICKET_SIZE])
        t.cert_chain = CertificateChain()
        t.cert_chain.load(build_cert_chain(raw_tmd, cetk))
        t.load_content_records()
        got_tid = str(t.tmd.title_id).lower()
        got_ver = int(t.tmd.title_version)
        tik_tid = bytes(t.ticket.title_id).hex()
        is_vwii = bool(t.tmd.vwii)
    except NusError:
        raise
    except Exception as e:
        raise NusError(f"Nintendo's server sent data for {title_id} that can't be read ({e}). "
                       "Check your internet connection (a Wi-Fi login page can cause this).") from e
    want = expected_title_id(title_id)
    if got_tid != want or tik_tid != want:
        raise NusError(f"Asked Nintendo's server for title {title_id} but received "
                       f"{got_tid}; the download is not genuine")
    if title_id.lower()[:8] in (HIGH_WII_SYSTEM, HIGH_VWII_SYSTEM) and \
            is_vwii != (title_id.lower()[:8] == HIGH_VWII_SYSTEM):
        raise NusError(f"{title_id}: received the title for the wrong console type; "
                       "the download is not genuine")
    if version is not None and got_ver != version:
        raise NusError(f"Asked Nintendo's server for {title_id} v{version} but received "
                       f"v{got_ver}; the download is not genuine")
    try:
        signed = bool(t.get_is_signed())
    except Exception:
        signed = False
    if not signed:
        raise NusError(f"{title_id}: Nintendo's signature did not verify; the download is not genuine")
    return t


def _fetch_content(title_id: str, t: Title, rec, rep: Reporter | None) -> tuple[bytes, bytes]:
    """Download one content and check it: ``(encrypted, decrypted)``.

    The size must be exactly what the signed TMD says and the SHA-1 must match.
    """
    size = _padded(rec.content_size)
    if size > MAX_CONTENT_BYTES:
        raise NusError(f"{title_id}: content {rec.content_id:08x} is unreasonably large")
    data = _fetch(_url(title_id, f"{rec.content_id:08x}"), rep, max_bytes=size)
    if data is None or len(data) != size:
        raise NusError(f"{title_id}: content {rec.content_id:08x} has the wrong size")
    try:
        dec = decrypt_content(data, t.ticket.get_title_key(), rec.index, rec.content_size)
        ok = hashlib.sha1(dec).hexdigest() == rec.content_hash.decode().lower()
    except Exception as e:
        raise NusError(f"{title_id}: content {rec.content_id:08x} could not be decrypted ({e})") from e
    if not ok:
        raise NusError(f"{title_id}: content {rec.content_id:08x} failed its checksum")
    return data, dec


def download_title(title_id: str, version: Optional[int], progress: ProgressFn | None = None,
                   cancel: CancelToken | None = None) -> Title:
    """Download a title (TMD, ticket, certs, all contents) and verify it.

    Raises :class:`NusError` if it has no public ticket, the server answers with
    a different title or version, Nintendo's signature doesn't verify, or a
    content's size or SHA-1 doesn't match the TMD.
    """
    rep = Reporter(progress, cancel)
    rep.note(f"Downloading {title_id} metadata")
    rep.check()
    t = load_verified(title_id, version)
    rep.check()
    recs = t.tmd.content_records
    rep.set_total(sum(_padded(r.content_size) for r in recs), f"Downloading {title_id}")
    contents: list[bytes] = []
    for i, rec in enumerate(recs, start=1):
        rep.note(f"Downloading content {i} of {len(recs)}")
        contents.append(_fetch_content(title_id, t, rec, rep)[0])
    t.content.content_list = contents
    return t


def rekey_for_wii(title: Title) -> None:
    """Re-encrypt a vWii title key with the Wii common key (index 0), like NUSGet.

    vWii mode can't use the vWii key itself, so installers there need this.
    This invalidates Nintendo's ticket signature (same as NUSGet does); the
    d2x installer doesn't need it.
    """
    if title.ticket.common_key_index == 0:
        return
    key = title.ticket.get_title_key()
    title.ticket.title_key_enc = encrypt_title_key(key, 0, title.tmd.title_id)
    title.ticket.common_key_index = 0


def download_title_wad(title_id: str, version: Optional[int], dest: Path | str,
                       progress: ProgressFn | None = None, cancel: CancelToken | None = None, *,
                       wii_common_key: bool = False, settings: Settings | None = None) -> Path:
    """Download a title from NUS and write it as an installable WAD at ``dest``.

    Low-level: it does not know what the title is for. Pass ``settings`` to run
    :func:`rewii.core.wad.classify_risk` on the result and raise :class:`Refused`
    (writing nothing) if it would be unsafe for the user's console.
    """
    dest = Path(dest)
    title = download_title(title_id, version, progress, cancel)
    if wii_common_key:
        rekey_for_wii(title)
    if settings is not None:
        info = info_from_title(title, verify_hashes=False)
        info.hashes_ok = True          # download_title checked every content's SHA-1
        ensure_allowed(info, settings)
    data = title.dump_wad()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def _existing_ok(path: Path, entry: CatalogEntry) -> bool:
    """True if ``path`` already holds exactly this title/version with good contents.

    Titles ReWii doesn't re-key must still carry Nintendo's signature, so a
    modified WAD that merely has the right name/ID/version is downloaded again.
    """
    if not path.is_file():
        return False
    from .wad import WadError, inspect_wad
    try:
        info = inspect_wad(path, verify_hashes=True)
    except WadError:
        return False
    if info.title_id != expected_title_id(entry.title_id) or info.version != entry.version:
        return False
    if not info.hashes_ok or info.platform != entry.platform:
        return False
    if entry.wii_common_key:
        return info.common_key_index == 0
    return info.signed


def _fetch_entries(entries: list[CatalogEntry], folder: Path, settings: Settings,
                   progress: ProgressFn | None, cancel: CancelToken | None) -> list[Path]:
    out: list[Path] = []
    for i, e in enumerate(entries, start=1):
        dest = folder / e.filename
        if _existing_ok(dest, e):
            out.append(dest)
            continue

        def sub(ev, i=i, e=e):  # prefix messages with "(1/4) IOS38 ..."
            if progress is not None:
                from .progress import ProgressEvent
                progress(ProgressEvent(ev.done, ev.total, f"({i}/{len(entries)}) {e.description}: {ev.message}"))

        out.append(download_title_wad(e.title_id, e.version, dest, sub, cancel,
                                      wii_common_key=e.wii_common_key, settings=settings))
    return out


def cios_entries(settings: Settings) -> list[CatalogEntry]:
    """The base IOS the d2x installer needs for this console (refuses Wii mini)."""
    plat = require_profile(settings, ("wii", "family", "vwii"), "cIOS files")
    return CIOS_BASE[plat]


def prepare_cios_files(layout: DriveLayout, settings: Settings, progress: ProgressFn | None = None,
                       cancel: CancelToken | None = None) -> list[Path]:
    """Put the d2x cIOS base WADs (``IOS38-64-v4123.wad`` ...) on the drive root.

    Wii / Family Edition get the Wii IOS; vWii gets the vWii IOS re-encrypted
    with the Wii common key. Wii mini is refused: its d2xl installer brings
    its own files. Files already present and valid are kept.
    """
    entries = cios_entries(settings)
    return _fetch_entries(entries, layout.root, settings, progress, cancel)


def update_43_entries(settings: Settings) -> list[CatalogEntry]:
    """System Menu 4.3 for the console's region + IOS80 + IOS58 (original Wii only)."""
    require_profile(settings, ("wii", "family"), "the Wii Menu 4.3 update")
    if settings.console_region not in SYSTEM_MENU_43:
        raise Refused(f"Unknown console region {settings.console_region!r}.")
    return [*UPDATE_43_IOS, system_menu_43(settings.console_region)]


def prepare_update_43(layout: DriveLayout, settings: Settings, progress: ProgressFn | None = None,
                      cancel: CancelToken | None = None) -> list[Path]:
    """Stage the Wii Menu 4.3 update WADs in ``wad/`` for YAWMME.

    Refused for vWii and Wii mini, and when the console region isn't set.
    Install order (guide): IOS80 and IOS58 first, then the System Menu.
    """
    entries = update_43_entries(settings)
    return _fetch_entries(entries, layout.wad, settings, progress, cancel)


def theme_base(settings: Settings) -> ThemeBase:
    """The base theme entry for this console (requires Wii Menu 4.3)."""
    plat = require_profile(settings, ("wii", "family", "vwii"), "the base theme")
    tb = THEME_BASE.get((plat, settings.console_region))
    if tb is None:
        raise Refused(f"There is no base theme for a {settings.console_region} {settings.console_type} "
                      "console.")
    return tb


def download_theme_base(settings: Settings, dest_dir: Path | str, progress: ProgressFn | None = None,
                        cancel: CancelToken | None = None) -> Path:
    """Download and decrypt the Wii Menu base theme ``.app`` into ``dest_dir``.

    Only valid if the console runs Wii Menu 4.3 (vWii always does). Usually
    ``dest_dir`` is ``<drive>/themes`` for csm-installer / ThemeMii.
    """
    tb = theme_base(settings)
    dest = Path(dest_dir) / tb.filename
    rep = Reporter(progress, cancel)
    rep.check()
    t = load_verified(tb.title_id, tb.version)
    try:
        rec = next(r for r in t.tmd.content_records if r.content_id == tb.content_id)
    except StopIteration:
        raise NusError(f"Content {tb.content_id:08X} is not part of Wii Menu v{tb.version}") from None
    rep.set_total(_padded(rec.content_size), f"Downloading base theme {tb.filename}")
    _enc, dec = _fetch_content(tb.title_id, t, rec, rep)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        tmp.write_bytes(dec)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest
