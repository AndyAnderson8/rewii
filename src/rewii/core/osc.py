"""Open Shop Channel (OSC) homebrew: browse the index, install/update/remove apps.

Index: ``https://hbb1.oscwii.org/api/v4/contents`` (a JSON list, cached daily).
Each app's archive is a zip laid out from the drive root (``apps/<folder>/...``
plus any extra top-level folders such as ``controllers/`` for Nintendont); its
``assets.archive.hash`` is an MD5.

Install records (slug, version, hash) are kept in ``rewii/osc_installed.json`` on
the drive so updates can be detected reliably.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from collections.abc import Iterable, Sequence
from xml.etree import ElementTree as ET

from . import archive, config, console, fileops, net
from .archive import MAX_UNCOMPRESSED
from .fileops import explain_os_error
from .layout import APPS_DIR, REWII_DIR, DriveLayout
from .progress import UNIT_STEPS, CancelToken, ProgressEvent, ProgressFn
from .errors import ReWiiError

OSC_API = "https://hbb1.oscwii.org/api/v4/contents"
INDEX_MAX_AGE = 86400
RECORD_NAME = "osc_installed.json"
MAX_INDEX_BYTES = 32 * 1024 ** 2
MAX_ARCHIVE_BYTES = 2 * 1024 ** 3     # largest app zip ReWii will download
PLATFORMS = ("wii", "vwii", "wii_mini")
# Settings.console_type -> OSC platform name
PLATFORM_BY_CONSOLE = {**{ct: console.osc_platform(ct) for ct in console.CONSOLE_TYPES},
                       "wii_mini": "wii_mini", "": ""}


class OSCError(ReWiiError):
    """Bad archive or install problem."""


@dataclass
class App:
    """One app in the Open Shop Channel index."""

    slug: str
    name: str
    author: str = ""
    version: str = ""
    category: str = ""
    description_short: str = ""
    description_long: str = ""
    supported_platforms: list[str] = field(default_factory=list)
    file_size: int = 0                 # archive (zip) size
    uncompressed_size: int = 0
    archive_url: str = ""
    archive_hash: str = ""             # md5 of the zip
    binary_hash: str = ""              # md5 of the app's boot.dol/boot.elf
    icon_url: str = ""
    subdirectories: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)   # e.g. WRITES_TO_NAND, DEPRECATED
    package_type: str = ""             # dol / elf
    peripherals: list[str] = field(default_factory=list)
    release_date: int = 0
    downloads: int = 0

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> App:
        """Build an App from one index entry, tolerating missing or mistyped fields."""
        def dct(v: Any) -> dict[str, Any]:
            return v if isinstance(v, dict) else {}

        def num(v: Any) -> int:
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0

        def strs(v: Any) -> list[str]:
            return [str(x) for x in v] if isinstance(v, (list, tuple)) else []

        assets = dct(d.get("assets"))
        archive_ = dct(assets.get("archive"))
        desc = dct(d.get("description"))
        return cls(
            slug=str(d.get("slug", "")),
            name=str(d.get("name") or d.get("slug", "")),
            author=str(d.get("author") or ""),
            version=str(d.get("version") or ""),
            category=str(d.get("category") or ""),
            description_short=str(desc.get("short") or ""),
            description_long=str(desc.get("long") or ""),
            supported_platforms=strs(d.get("supported_platforms")),
            file_size=num(archive_.get("size")),
            uncompressed_size=num(d.get("uncompressed_size")),
            archive_url=str(archive_.get("url") or ""),
            archive_hash=str(archive_.get("hash") or ""),
            binary_hash=str(dct(assets.get("binary")).get("hash") or ""),
            icon_url=str(dct(assets.get("icon")).get("url") or ""),
            subdirectories=strs(d.get("subdirectories")),
            flags=strs(d.get("flags")),
            package_type=str(d.get("package_type") or ""),
            peripherals=strs(d.get("peripherals")),
            release_date=num(d.get("release_date")),
            downloads=num(d.get("downloads")),
        )

    @property
    def app_folders(self) -> list[str]:
        """Folder names this app creates under ``apps/`` (from ``subdirectories``)."""
        out: list[str] = []
        for s in self.subdirectories:
            parts = [p for p in s.replace("\\", "/").split("/") if p]
            if (len(parts) >= 2 and parts[0].lower() == APPS_DIR and parts[1] not in out
                    and _plain_name(parts[1])):
                out.append(parts[1])
        return out or [self.slug]

    @property
    def allowed_dirs(self) -> list[str]:
        """Drive-relative folders (lower-case POSIX) this app may write into: every
        ``apps/<folder>`` it owns plus the ``subdirectories`` the index declares.
        ReWii's own ``rewii/`` folder is never included."""
        out = [f"{APPS_DIR}/{f.lower()}" for f in self.app_folders if _plain_name(f)]
        for s in self.subdirectories:
            parts = [p for p in s.replace("\\", "/").split("/") if p]
            if not parts or not all(_plain_name(p) for p in parts):
                continue
            d = "/".join(parts).lower()
            if d == APPS_DIR or parts[0].lower() == RESERVED_TOP or d in out:
                continue
            out.append(d)
        return out

    @property
    def folder(self) -> str:
        """Main ``apps/<folder>`` name."""
        return self.app_folders[0]

    @property
    def writes_to_nand(self) -> bool:
        return "WRITES_TO_NAND" in self.flags


    def supports(self, platform: str) -> bool:
        p = PLATFORM_BY_CONSOLE.get(platform, platform)
        return not p or not self.supported_platforms or p in self.supported_platforms


def _plain_name(part: str) -> bool:
    """A single safe folder name (no separators, ``..``, drive letters, reserved names)."""
    try:
        return archive.safe_parts(part) == [part]
    except archive.ArchiveError:
        return False


RESERVED_TOP = REWII_DIR     # ReWii's own records; no app may write there


def _index_ok(p: Path) -> bool:
    try:
        return isinstance(json.loads(p.read_text("utf-8")), list)
    except (OSError, ValueError):
        return False


def fetch_index(refresh: bool = False) -> list[App]:
    """The OSC app list (cached for a day; stale cache used when offline).

    Raises :class:`net.NetError` when offline with no cache, or if the data is invalid.
    """
    p = net.cached(OSC_API, "osc/contents.json", INDEX_MAX_AGE, refresh, validate=_index_ok,
                   max_bytes=MAX_INDEX_BYTES)
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError) as e:
        p.unlink(missing_ok=True)
        raise net.NetError(f"Open Shop Channel index is unreadable: {e}") from e
    if not isinstance(data, list):
        p.unlink(missing_ok=True)
        raise net.NetError("Open Shop Channel index has an unexpected format")
    return [App.from_api(d) for d in data if isinstance(d, dict) and d.get("slug")]


def find_app(apps: Iterable[App], slug: str) -> Optional[App]:
    s = slug.lower()
    return next((a for a in apps if a.slug.lower() == s), None)


def search(apps: Iterable[App], text: str = "", category: Optional[str] = None,
           platform: Optional[str] = None) -> list[App]:
    """Filter by words (name/slug/author/description), category and console platform.

    Ranked: name matches first, then description matches; ties by name.
    """
    words = text.casefold().split()
    out: list[tuple[int, str, App]] = []
    for a in apps:
        if category and a.category != category:
            continue
        if platform and not a.supports(platform):
            continue
        head = f"{a.name} {a.slug} {a.author}".casefold()
        body = f"{a.description_short} {a.description_long}".casefold()
        if words:
            if all(w in head for w in words):
                rank = 0 if a.name.casefold().startswith(words[0]) else 1
            elif all(w in head or w in body for w in words):
                rank = 2
            else:
                continue
        else:
            rank = 0
        out.append((rank, a.name.casefold(), a))
    out.sort(key=lambda t: (t[0], t[1]))
    return [a for _, _, a in out]


# --------------------------------------------------------------------------- installed apps
@dataclass
class InstalledApp:
    """An app folder found on the drive."""

    folder: str
    path: Path
    name: str = ""
    version: str = ""
    coder: str = ""
    short_description: str = ""
    boot_file: Optional[str] = None       # boot.dol / boot.elf, None if missing
    nested: bool = False                  # wrongly placed, e.g. apps/apps/<x>
    slug: str = ""                        # from ReWii's install record, if any
    archive_hash: str = ""

    @property
    def bootable(self) -> bool:
        return self.boot_file is not None and not self.nested

    @property
    def display_name(self) -> str:
        return self.name or self.folder


def _meta_text(raw: str, tag: str) -> str:
    m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", raw, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def read_meta(path: Path) -> dict[str, str]:
    """name/version/coder/short_description from a HBC ``meta.xml`` (tolerates bad XML)."""
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    out: dict[str, str] = {}
    try:
        root = ET.fromstring(raw)
        for k in ("name", "version", "coder", "short_description"):
            out[k] = " ".join((root.findtext(k) or "").split())
        if not out.get("coder"):
            out["coder"] = " ".join((root.findtext("author") or "").split())
        return out
    except (ET.ParseError, ValueError, LookupError):
        out = {}                     # bad XML / unknown encoding: fall back to regexes
    text = raw.decode("utf-8", errors="replace")
    for k in ("name", "version", "coder", "short_description"):
        out[k] = _meta_text(text, k)
    if not out["coder"]:
        out["coder"] = _meta_text(text, "author")
    return out


def _boot_file(folder: Path) -> Optional[str]:
    try:
        names = {p.name.lower(): p.name for p in folder.iterdir() if p.is_file()}
    except OSError:
        return None
    for want in ("boot.dol", "boot.elf"):
        if want in names:
            return names[want]
    return None


def _load_records(layout: DriveLayout) -> dict[str, dict[str, str]]:
    try:
        data = json.loads((layout.rewii / RECORD_NAME).read_text("utf-8"))
        if not isinstance(data, dict):
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def _save_records(layout: DriveLayout, rec: dict[str, dict[str, str]]) -> None:
    layout.require_root()
    layout.rewii.mkdir(parents=True, exist_ok=True)
    fileops.write_atomic(layout.rewii / RECORD_NAME, json.dumps(rec, indent=2, sort_keys=True))


def _scan(folder: Path, nested: bool, records: dict[str, dict[str, str]]) -> InstalledApp:
    meta = read_meta(folder / "meta.xml")
    rec = records.get(folder.name.lower(), {}) if not nested else {}
    return InstalledApp(folder=folder.name, path=folder, name=meta.get("name", ""),
                        version=meta.get("version", ""), coder=meta.get("coder", ""),
                        short_description=meta.get("short_description", ""),
                        boot_file=_boot_file(folder), nested=nested,
                        slug=str(rec.get("slug") or ""), archive_hash=str(rec.get("hash") or ""))


def installed_apps(layout: DriveLayout) -> list[InstalledApp]:
    """Scan ``apps/*`` (meta.xml, boot.dol/.elf). Misplaced apps found in
    ``apps/apps/*`` or ``apps/<x>/apps/*`` are included with ``nested=True``."""
    out: list[InstalledApp] = []
    if not layout.apps.is_dir():
        return out
    records = _load_records(layout)
    for d in sorted(layout.apps.iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir():
            continue
        if d.name.lower() == APPS_DIR:
            for sub in sorted(d.iterdir(), key=lambda p: p.name.lower()):
                if sub.is_dir():
                    out.append(_scan(sub, True, records))
            continue
        out.append(_scan(d, False, records))
        inner = d / APPS_DIR
        if inner.is_dir() and _boot_file(d) is None:
            for sub in sorted(inner.iterdir(), key=lambda p: p.name.lower()):
                if sub.is_dir():
                    out.append(_scan(sub, True, records))
    return out


# --------------------------------------------------------------------------- install
def _safe_parts(name: str) -> Optional[list[str]]:
    """Validated path components of a zip entry, None for directory entries.

    Raises OSCError for absolute paths, drive letters, ``..``, bad characters or
    Windows reserved names (see :func:`rewii.core.archive.safe_parts`).
    """
    try:
        return archive.safe_parts(name)
    except archive.ArchiveError as e:
        raise OSCError(str(e)) from e


def plan_archive(names: Sequence[str], default_folder: str) -> dict[str, str]:
    """Map zip entry names to drive-relative POSIX paths.

    * ``apps/<x>/...`` (and other top-level folders) are kept as-is,
    * a single wrapper folder around ``apps/`` is stripped,
    * a bare app (``boot.dol`` at the zip root, or ``<x>/boot.dol``) goes to ``apps/``.
    """
    entries: list[tuple[str, list[str]]] = []
    for n in names:
        parts = _safe_parts(n)
        if parts is not None:
            entries.append((n, parts))
    if not entries:
        return {}
    tops = {p[0].lower() for _, p in entries}
    # strip a wrapper folder (e.g. "MyApp-1.0/apps/MyApp/boot.dol")
    if len(tops) == 1 and APPS_DIR not in tops and all(len(p) > 1 for _, p in entries):
        if any(p[1].lower() == APPS_DIR for _, p in entries):
            entries = [(n, p[1:]) for n, p in entries]
            tops = {p[0].lower() for _, p in entries}
    if APPS_DIR not in tops:
        boots = [p for _, p in entries if p[-1].lower() in ("boot.dol", "boot.elf")]
        if any(len(p) == 1 for p in boots):
            entries = [(n, [APPS_DIR, default_folder, *p]) for n, p in entries]
        elif boots and len(tops) == 1 and all(len(p) == 2 for p in boots):
            entries = [(n, [APPS_DIR, *p]) for n, p in entries]
    plan: dict[str, str] = {}
    for n, p in entries:
        if p[0].lower() == APPS_DIR:
            p = [APPS_DIR, *p[1:]]
        plan[n] = "/".join(p)
    return plan


def _allow_fn(allowed: Optional[Iterable[str]]):
    if allowed is None:
        return None
    dirs = [d.strip("/").lower() for d in allowed if d.strip("/")]

    def allow(rel: str) -> bool:
        low = rel.lower()
        if low.split("/", 1)[0] == RESERVED_TOP:
            return False
        return any(low.startswith(d + "/") for d in dirs)
    return allow


def extract_archive(zip_path: Path, dest_root: Path, default_folder: str,
                    progress: Optional[ProgressFn] = None,
                    cancel: Optional[CancelToken] = None, *,
                    allowed: Optional[Iterable[str]] = None,
                    skipped: Optional[list[str]] = None) -> list[Path]:
    """Safely extract an app zip onto the drive root. Returns the files written.

    ``allowed`` (see :attr:`App.allowed_dirs`) limits extraction to those
    drive-relative folders; other entries are not written and their destinations
    are appended to ``skipped`` when a list is passed. Unsafe archives (path
    tricks, reserved names, duplicate names, oversized) raise :class:`OSCError`
    before anything is written.
    """
    try:
        res = archive.extract_zip(zip_path, dest_root,
                                  mapping=lambda names: plan_archive(names, default_folder),
                                  allow=_allow_fn(allowed), max_total=MAX_UNCOMPRESSED,
                                  progress=progress, cancel=cancel)
    except archive.ArchiveError as e:
        raise OSCError(str(e)) from e
    if skipped is not None:
        skipped.extend(res.skipped)
    return res.written


@dataclass
class InstallResult:
    app: App
    files: list[Path]
    skipped: list[str] = field(default_factory=list)   # zip entries outside the app's folders

    @property
    def folder(self) -> str:
        return self.app.folder


def download_archive(app: App, progress: Optional[ProgressFn] = None,
                     cancel: Optional[CancelToken] = None, refresh: bool = False) -> Path:
    """Download (or reuse the cached) app zip, verifying its MD5."""
    if not app.archive_url:
        raise OSCError(f"{app.name} has no download")
    tag = (app.archive_hash or app.version or "latest")[:12]
    safe_slug = re.sub(r"[^A-Za-z0-9._-]", "_", app.slug)
    dest = config.cache_dir() / "osc" / "zips" / f"{safe_slug}-{tag}.zip"
    if dest.is_file() and not refresh and _archive_ok(app, dest):
        return dest
    if not app.archive_url.lower().startswith("https://"):
        raise OSCError(f"{app.name}: refusing a download that isn't https")
    net.download(app.archive_url, dest, progress=progress, cancel=cancel,
                 max_bytes=MAX_ARCHIVE_BYTES)
    if not _archive_ok(app, dest):
        dest.unlink(missing_ok=True)
        raise OSCError(f"{app.name}: the download is damaged (checksum mismatch)")
    return dest


def _archive_ok(app: App, zip_path: Path) -> bool:
    """Zip MD5 matches - or, when the shop repacked the zip without updating its
    hash, the app's own ``apps/<folder>/boot.dol|elf`` matches the published binary
    hash.

    The fallback only vouches for that one file, so it is strict: the match must
    be the boot file that will actually be installed for this app (after
    :func:`plan_archive`), it must be the only boot file in that folder, and the
    archive must pass every safety check (no duplicate names etc.). Everything
    else in the zip stays confined by :attr:`App.allowed_dirs` at extraction.
    An app without a published zip hash can't be verified at all and is accepted.
    """
    if not app.archive_hash:
        return True
    if fileops.hash_file(zip_path, "md5") == app.archive_hash.lower():
        return True
    if not app.binary_hash or not _plain_name(app.folder):
        return False
    base = f"{APPS_DIR}/{app.folder.lower()}/"
    try:
        with zipfile.ZipFile(zip_path) as zf:
            plan = archive.plan_zip(zf, lambda names: plan_archive(names, app.folder))
            boots = [info for info, rel in plan
                     if rel.lower() in (base + "boot.dol", base + "boot.elf")]
            if len(boots) != 1:
                return False
            h = hashlib.md5()
            with zf.open(boots[0]) as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest() == app.binary_hash.lower()
    except (OSError, zipfile.BadZipFile, archive.ArchiveError, OSCError):
        return False


def _phase(progress: Optional[ProgressFn], start: float, span: float, label: str
           ) -> Optional[ProgressFn]:
    """Map a sub-operation's progress into [start, start+span] of 1000 units."""
    if progress is None:
        return None

    def fn(ev: ProgressEvent) -> None:
        progress(ProgressEvent(int((start + span * ev.fraction) * 1000), 1000,
                               f"{label}: {ev.message}" if ev.message else label, UNIT_STEPS))
    return fn


def install_app(layout: DriveLayout, app: App, progress: Optional[ProgressFn] = None,
                cancel: Optional[CancelToken] = None, *, refresh: bool = False) -> InstallResult:
    """Download, verify and extract ``app`` onto the drive (overwriting older files).

    Progress is reported as 0..1000 (download = first half, extract = second half).
    """
    zpath = download_archive(app, _phase(progress, 0.0, 0.5, f"Downloading {app.name}"),
                             cancel, refresh)
    if not _plain_name(app.folder):
        raise OSCError(f"{app.name}: invalid app folder name {app.folder!r}")
    skipped: list[str] = []
    layout.require_root()
    # folders this install creates: if it fails they must not stay behind empty
    # (an empty apps/<name> shows up as a broken app)
    fresh = [d for d in (layout.apps / f for f in app.app_folders) if not d.exists()]
    try:
        files = extract_archive(zpath, layout.root, app.folder,
                                _phase(progress, 0.5, 0.5, f"Installing {app.name}"), cancel,
                                allowed=app.allowed_dirs, skipped=skipped)
        if not any(f.name.lower() in ("boot.dol", "boot.elf") for f in files):
            raise OSCError(f"{app.name}: the download has no boot.dol/boot.elf for apps/{app.folder}")
    except BaseException:
        for d in fresh:
            _prune_empty(d)
        raise
    rec = _load_records(layout)
    for folder in app.app_folders:
        rec[folder.lower()] = {"slug": app.slug, "version": app.version,
                               "hash": app.archive_hash, "folder": folder}
    _save_records(layout, rec)
    return InstallResult(app, files, skipped)


def _prune_empty(folder: Path) -> None:
    """Remove ``folder`` if it holds nothing but empty folders (never any file)."""
    try:
        if not folder.is_dir() or any(p.is_file() or p.is_symlink() for p in folder.rglob("*")):
            return
        for d in sorted((p for p in folder.rglob("*") if p.is_dir()), key=lambda x: -len(x.parts)):
            d.rmdir()
        folder.rmdir()
    except OSError:
        pass


def remove_app(layout: DriveLayout, folder: str) -> bool:
    """Delete ``apps/<folder>``. Extra top-level folders an app created (e.g.
    ``controllers/``) are left alone. Returns False if it wasn't there."""
    if not folder or folder in (".", "..") or re.search(r"[\\/:]", folder):
        raise OSCError(f"invalid app folder name: {folder!r}")
    target = layout.apps / folder
    if not target.is_dir():
        return False
    if not target.resolve().is_relative_to(layout.apps.resolve()):
        raise OSCError(f"refusing to remove {target}")
    shutil.rmtree(target)
    rec = _load_records(layout)
    if rec.pop(folder.lower(), None) is not None:
        _save_records(layout, rec)
    return True


def _norm_version(v: str) -> str:
    return re.sub(r"\s+", " ", v.strip().casefold().lstrip("v"))


def match_app(inst: InstalledApp, apps: Iterable[App]) -> Optional[App]:
    """The OSC app an installed folder came from (by install record, then folder)."""
    apps = list(apps)
    if inst.slug:
        a = find_app(apps, inst.slug)
        if a is not None:
            return a
    f = inst.folder.lower()
    for a in apps:
        if f in (x.lower() for x in a.app_folders):
            return a
    return find_app(apps, inst.folder)


def update_available(installed: Iterable[InstalledApp], apps: Iterable[App]
                     ) -> dict[str, App]:
    """``{folder: App}`` for installed apps whose OSC version differs.

    Uses the archive hash recorded at install time when available, else compares
    the meta.xml version with the index version.
    """
    apps = list(apps)
    out: dict[str, App] = {}
    for inst in installed:
        if inst.nested or inst.boot_file is None:
            continue   # not a working install (e.g. only covers in apps/usbloader_gx)
        a = match_app(inst, apps)
        if a is None:
            continue
        if inst.archive_hash and a.archive_hash:
            if inst.archive_hash.lower() != a.archive_hash.lower():
                out[inst.folder] = a
        elif a.version and _norm_version(inst.version) != _norm_version(a.version):
            out[inst.folder] = a
    return out


# --------------------------------------------------------------------------- starter kit
@dataclass(frozen=True)
class StarterItem:
    """One app in the ReWii starter kit."""

    slug: str
    essential: bool
    why: str
    platforms: tuple[str, ...] = PLATFORMS     # restrict beyond OSC's own list
    name: str = ""                             # display name ("" = the slug)
    required: bool = False                     # the drive can't play games without it

    @property
    def title(self) -> str:
        return self.name or self.slug

    @property
    def folder(self) -> str:
        """Its folder under ``apps/`` (the shop installs each app as ``apps/<slug>``)."""
        return self.slug


#: THE list of apps ReWii sets a drive up with. "Set up" installs the essential ones,
#: the drive health check looks for them (``health.check_apps``), and the guides'
#: "prepare" buttons install theirs from here.
STARTER_KIT: tuple[StarterItem, ...] = (
    StarterItem("usbloader_gx", True, "Game loader (Wii + GameCube via Nintendont)",
                name="USB Loader GX", required=True),
    StarterItem("Nintendont", True, "Plays GameCube games from USB", required=True),
    StarterItem("yawmME", True, "Installs WADs/channels from the wad/ folder", name="YAWMME"),
    StarterItem("SaveGame_Manager_GX", True, "Backs up and restores Wii saves on USB",
                name="SaveGame Manager GX"),
    StarterItem("wiixplorer", True, "File manager on the Wii", name="WiiXplorer"),
    StarterItem("d2x-cios-installer", True, "Installs d2x cIOS needed by USB loaders",
                ("wii",), name="d2x cIOS Installer"),
    StarterItem("d2x-cios-installer-vwii", True, "Installs d2x cIOS on a Wii U (vWii)",
                ("vwii",), name="d2x cIOS Installer (vWii)"),
    StarterItem("priiloader", True, "Brick protection; also patches the System Menu", name="Priiloader"),
    StarterItem("nanddumper_ios", True, "Backs up the console's NAND to USB", name="NAND Dumper"),
    StarterItem("SysCheckME", True, "Reports installed IOS/cIOS for health checks", name="SysCheck ME"),
    StarterItem("LoadPriiloader", False, "Opens Priiloader from the Homebrew Channel"),
    StarterItem("CleanRip", False, "Dumps your own Wii/GameCube discs to USB"),
    StarterItem("sntp", False, "Sets the Wii clock from the internet"),
    StarterItem("WiiLink-WFC-Patcher", False, "Patches WiiWare/channels for WiiLink WFC"),
    StarterItem("Mail-Patcher", False, "WiiLink Wii Mail setup"),
    StarterItem("wiilink-account-linker", False, "Links the console to a WiiLink account"),
)
"""Starter kit slugs (verified against the live OSC index, 2026-11).

Not on OSC (handled elsewhere): the Homebrew Channel/BootMii (HackMii installer),
the WiiLink patcher/installer (a PC app), d2xl cIOS for the Wii mini, ModMii.
"""


def starter_kit(platform: str = "", which: str | Iterable[str] = "essential"
                ) -> list[StarterItem]:
    """Kit items for ``platform`` (Settings.console_type or OSC name).

    ``which`` is "essential", "recommended" (non-essential only), "all", or an
    iterable of slugs.
    """
    p = PLATFORM_BY_CONSOLE.get(platform, platform)
    if isinstance(which, str):
        if which == "essential":
            items = [i for i in STARTER_KIT if i.essential]
        elif which == "recommended":
            items = [i for i in STARTER_KIT if not i.essential]
        elif which == "all":
            items = list(STARTER_KIT)
        else:
            raise OSCError(f"unknown starter kit selection: {which!r}")
    else:
        wanted = {s.lower() for s in which}
        items = [i for i in STARTER_KIT if i.slug.lower() in wanted]
    if not p:
        # console type not set: leave out what only suits one console (the cIOS
        # installers) rather than putting both on the drive
        return [i for i in items if i.platforms == PLATFORMS]
    return [i for i in items if p in i.platforms]


def console_specific_items(which: str | Iterable[str] = "essential") -> list[StarterItem]:
    """Kit items that depend on the console type (left out until it is known)."""
    if isinstance(which, str):
        pool = [i for i in STARTER_KIT if which == "all" or i.essential == (which == "essential")]
    else:
        wanted = {s.lower() for s in which}
        pool = [i for i in STARTER_KIT if i.slug.lower() in wanted]
    return [i for i in pool if i.platforms != PLATFORMS]


@dataclass
class StarterKitReport:
    installed: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)   # not for this console
    not_found: list[str] = field(default_factory=list)     # no longer on OSC
    failed: dict[str, str] = field(default_factory=dict)
    needs_console: list[str] = field(default_factory=list)  # left out: console type not set


def kit_lines(rep: StarterKitReport) -> tuple[list[str], bool]:
    """Plain-language lines for an ``osc.StarterKitReport`` and whether anything went wrong."""
    failed = dict(rep.failed)
    lines = []
    if rep.installed:
        lines.append("Installed: " + ", ".join(rep.installed))
    elif not (failed or rep.not_found or rep.unsupported):
        lines.append("Everything was already installed and up to date.")
    else:
        lines.append("Nothing was installed.")
    if failed:
        lines.append("Failed:\n" + "\n".join(f"  {k}: {v}" for k, v in failed.items()))
    if rep.not_found:
        lines.append("Not on the Open Shop Channel any more: " + ", ".join(rep.not_found))
    if rep.unsupported:
        lines.append("Not for this console type (skipped): " + ", ".join(rep.unsupported))
    if rep.needs_console:
        lines.append("Left out because the console type isn't set yet: " + ", ".join(rep.needs_console)
                     + ".\nThe cIOS installer depends on the console (Wii, Wii U or Wii mini). Set the "
                     "console in Settings, then use System & Guides \u2192 cIOS \u2192 \"Prepare files "
                     "for this guide\" to put the right one on the drive.")
    return lines, bool(failed or rep.not_found)


def install_starter_kit(layout: DriveLayout, platform: str = "",
                        which: str | Iterable[str] = "essential",
                        progress: Optional[ProgressFn] = None,
                        cancel: Optional[CancelToken] = None, *,
                        apps: Optional[Sequence[App]] = None,
                        skip_current: bool = True) -> StarterKitReport:
    """Install the starter kit. Apps already installed at the latest version are
    skipped when ``skip_current``. One failing app doesn't stop the rest
    (cancellation does). Progress runs 0..1000 over the whole kit."""
    layout.require_root()
    apps = list(apps) if apps is not None else fetch_index()
    report = StarterKitReport()
    if not PLATFORM_BY_CONSOLE.get(platform, platform):
        report.needs_console = [i.slug for i in console_specific_items(which)]
    todo: list[App] = []
    p = PLATFORM_BY_CONSOLE.get(platform, platform)
    for item in starter_kit(platform, which):
        a = find_app(apps, item.slug)
        if a is None:
            report.not_found.append(item.slug)
        elif p and not a.supports(p):
            report.unsupported.append(item.slug)
        else:
            todo.append(a)
    if skip_current:
        inst = installed_apps(layout)
        outdated = update_available(inst, todo)
        have = {i.folder.lower() for i in inst if not i.nested and i.boot_file}
        stale = {o.slug for o in outdated.values()}
        todo = [a for a in todo if a.folder.lower() not in have or a.slug in stale]
    n = len(todo)
    for i, a in enumerate(todo):
        if cancel is not None:
            cancel.check()
        try:
            install_app(layout, a, _phase(progress, i / max(n, 1), 1 / max(n, 1),
                                          f"[{i + 1}/{n}] {a.name}"), cancel)
            report.installed.append(a.slug)
        except (net.NetError, OSCError, OSError) as e:
            report.failed[a.slug] = explain_os_error(e, layout.root)
            if not layout.connected():        # every other app would fail the same way
                break
    if progress is not None:
        progress(ProgressEvent(1000, 1000, "Starter kit done", UNIT_STEPS))
    return report
