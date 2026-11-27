"""Drive health check: does this USB drive match what the Wii (and wii.hacks.guide) expect?

:func:`check` returns a list of :class:`Finding` objects in plain language.
Findings whose ``fix_id`` is in :data:`SAFE_FIXES` can be repaired with
:func:`fix` (renames/moves on the same drive, deleting macOS junk, splitting
oversized files). Other ``fix_id`` values name an action the UI offers
elsewhere:

* ``format_drive``          - erase and format FAT32/MBR/32 KiB (``formatting``)
* ``install_starter_apps``  - install USB Loader GX, Nintendont, ... (homebrew module)
* ``convert_game``          - convert RVZ/WIA/NKit/GCZ or re-add the game (games module)
* ``import_wbfs``           - import games from an old raw WBFS partition
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Optional
from collections.abc import Callable, Iterator

from .disc import GC_DRIVE_EXTS, NEEDS_CONVERSION_EXTS, WII_DRIVE_EXTS, FAT32_SPLIT, DiscError, DiscInfo, NeedsConversion, disc_info, split_parts
from .drives import EXFAT, FAT16, FAT32, NTFS, UNKNOWN, WBFS, Drive, human_size
from .fat32 import MAX_SIZE, Fat32Error, plan_fat32
from .layout import APPS_DIR, MANAGED_TOPS, DriveLayout, game_folder_name, id_from_folder
from .osc import starter_kit
from . import fileops
from .fileops import (NEW_SUFFIX, PART_SUFFIX, TEMP_IDLE_SECONDS, TEMP_PREFIX, part_path, explain_os_error, is_inside, is_temp_name,
                      rename_path, same_file)
from .findings import SAFE_FIXES, Finding, Fix, Severity
from .progress import UNIT_STEPS, CancelToken, ProgressFn, Reporter
from .errors import ReWiiError

INFO, WARN, ERROR = Severity.INFO, Severity.WARNING, Severity.ERROR

FAT32_MAX_FILE = 0xFFFFFFFF          # 4 GiB - 1
RECOMMENDED_CLUSTER = 32768

#: A line of :func:`fix`'s log that starts with this reports something that could NOT be fixed.
NOT_FIXED = "couldn't fix:"


def expected_cluster(size: int) -> int:
    """The cluster size ReWii's own formatter picks for a drive of ``size`` bytes
    (32 KiB, less on drives of 2 GB and under where FAT32 doesn't allow that)."""
    try:
        return plan_fat32(size).cluster_size
    except (Fat32Error, ValueError):
        return RECOMMENDED_CLUSTER

# not ".Trashes": that holds files the user put in the Mac trash and may still want back
MAC_JUNK_DIRS = frozenset({".Spotlight-V100", ".fseventsd", ".TemporaryItems"})
MAC_JUNK_FILES = frozenset({".DS_Store"})

# tuples for str.endswith(); the sets themselves are defined in ``disc``
WII_EXTS = tuple(sorted(WII_DRIVE_EXTS))
GC_EXTS = tuple(sorted(GC_DRIVE_EXTS))
CONVERT_EXTS = (*sorted(NEEDS_CONVERSION_EXTS), ".nkit")
ARCHIVE_EXTS = (".zip", ".7z", ".rar")


class FixError(ReWiiError):
    """A fix couldn't be applied (conflict, unsupported fix, unreadable game...)."""


# --------------------------------------------------------------------------- drive-level
def check_drive(drive: Drive) -> list[Finding]:
    """Checks that only need the Drive: filesystem, partition style, clusters, size, free space."""
    out: list[Finding] = []
    fs = drive.filesystem
    if fs == NTFS:
        out.append(Finding("fs_ntfs", WARN,
                           "This drive uses NTFS. USB Loader GX can play Wii games from NTFS, but "
                           "the Homebrew Channel, Nintendont (GameCube games) and most apps need "
                           "FAT32. Reformatting to FAT32 is recommended.", Fix.FORMAT_DRIVE))
    elif fs == EXFAT:
        out.append(Finding("fs_exfat", ERROR,
                           "This drive uses exFAT, which the Wii can't read. It needs to be "
                           "reformatted to FAT32.", Fix.FORMAT_DRIVE))
    elif fs == WBFS:
        out.append(Finding("fs_wbfs", ERROR,
                           "This is an old WBFS-formatted drive. Modern loaders use FAT32. Import "
                           "your games to the computer first, then reformat the drive.", Fix.IMPORT_WBFS))
    elif fs == FAT16:
        out.append(Finding("fs_fat16", ERROR,
                           "This drive uses FAT16, which is too small for Wii games. It needs to "
                           "be reformatted to FAT32.", Fix.FORMAT_DRIVE))
    elif fs != FAT32:
        what = "no file system" if fs == UNKNOWN else fs
        extra = " (ReWii needs admin rights to look closer)" if drive.needs_admin else ""
        out.append(Finding("fs_other", ERROR,
                           f"This drive has {what}{extra}, which the Wii can't read. It needs to "
                           "be formatted as FAT32.", Fix.FORMAT_DRIVE))
    if drive.partition_style == "GPT":
        out.append(Finding("gpt", ERROR,
                           "This drive uses the GPT partition style. The Wii needs MBR. "
                           "Reformatting with ReWii fixes this.", Fix.FORMAT_DRIVE))
    cs = drive.cluster_size
    want = expected_cluster(drive.size) if drive.size else RECOMMENDED_CLUSTER
    # a small drive can't have 32 KB clusters: what ReWii's formatter chose is right for it
    if fs == FAT32 and cs and cs not in (RECOMMENDED_CLUSTER, want):
        out.append(Finding("cluster_size", WARN,
                           f"This drive uses {cs // 1024 or cs} {'KB' if cs >= 1024 else 'byte'} "
                           f"clusters. {want // 1024} KB clusters are recommended for the Wii on a "
                           "drive this size (faster loading, fewer problems). Reformatting fixes "
                           "this.", Fix.FORMAT_DRIVE, data={"cluster_size": cs, "expected": want}))
    if drive.size > MAX_SIZE:
        out.append(Finding("too_big", WARN,
                           f"This drive is {human_size(drive.size)}. A Wii-compatible (MBR) drive "
                           "can only use the first 2.2 TB.", None, data={"size": drive.size}))
    if drive.looks_like_flash:
        out.append(Finding("flash_stick", INFO,
                           "This looks like a USB flash stick. It works, but a hard drive or SSD "
                           "is faster and more reliable. Use the rear USB port nearest the edge."))
    free = drive.free
    if free is not None:
        if free < 1024 ** 3:
            out.append(Finding("low_space", WARN,
                               f"The drive is almost full ({human_size(free)} free).",
                               data={"free": free}))
        elif free < 8 * 1024 ** 3:
            out.append(Finding("low_space", INFO,
                               f"Only {human_size(free)} free: not enough for most Wii games "
                               "(up to 8 GB each).", data={"free": free}))
    return out


# --------------------------------------------------------------------------- layout-level
def _ci_child(d: Path, name: str) -> Optional[Path]:
    """Case-insensitive child lookup."""
    try:
        for p in d.iterdir():
            if p.name.lower() == name.lower():
                return p
    except OSError:
        return None
    return None


def _has_boot(d: Path) -> bool:
    return _ci_child(d, "boot.dol") is not None or _ci_child(d, "boot.elf") is not None


def _subdirs(d: Path) -> list[Path]:
    try:
        return sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")
                      and not is_temp_name(p.name))
    except OSError:
        return []


def _files(d: Path) -> list[Path]:
    try:
        # ReWii's own unfinished files are never games/apps (see check_temp_files)
        return sorted(p for p in d.iterdir() if p.is_file() and not p.name.startswith("._")
                      and not is_temp_name(p.name))
    except OSError:
        return []


def check_apps(layout: DriveLayout) -> list[Finding]:
    """Homebrew checks: nested apps folders, apps without boot.dol, missing starter apps."""
    out: list[Finding] = []
    apps = layout.apps
    if not apps.is_dir():
        out.append(Finding("no_apps", WARN,
                           "There's no apps folder, so the Homebrew Channel won't find any apps "
                           "on this drive.", Fix.INSTALL_STARTER_APPS))
        return out
    nested = _ci_child(apps, APPS_DIR)
    if nested is not None and nested.is_dir():
        out.append(Finding("nested_apps", ERROR,
                           "There's an apps folder inside the apps folder (apps/apps), so the "
                           "Homebrew Channel can't see those apps. ReWii can move them up.",
                           Fix.FLATTEN_NESTED_APPS, nested))
    present = set()
    data_only = {"images", "cache_bnr", "cache", "theme", "language", "gxglobal.cfg", "gxgamesettings.cfg"}
    for app in _subdirs(apps):
        if nested is not None and app.name == nested.name:      # reported above, once
            continue
        if _has_boot(app):
            present.add(app.name.lower())
            continue
        # loader data (covers, banner cache, settings) saved before the app itself is installed
        try:
            child_names = {c.name.lower() for c in app.iterdir()}
        except OSError:
            child_names = set()
        if child_names and child_names <= data_only:
            continue
        present.add(app.name.lower())
        inner_apps = _ci_child(app, APPS_DIR)
        if inner_apps is not None and inner_apps.is_dir():
            out.append(Finding("nested_apps", ERROR,
                               f"apps/{app.name} contains another apps folder; the app inside "
                               "won't show up. ReWii can move it to the right place.",
                               Fix.FLATTEN_NESTED_APPS, inner_apps))
            continue
        inner = [s for s in _subdirs(app) if _has_boot(s)]
        if len(inner) == 1:
            out.append(Finding("app_double_folder", WARN,
                               f"apps/{app.name} has its app one folder too deep "
                               f"(apps/{app.name}/{inner[0].name}). ReWii can move it up.",
                               Fix.FLATTEN_APP_FOLDER, inner[0]))
            present.add(inner[0].name.lower())
            continue
        out.append(Finding("app_no_boot", WARN,
                           f"apps/{app.name} has no boot.dol or boot.elf, so it won't start. "
                           "Reinstall the app.", None, app))
    if nested is not None and nested.is_dir():
        present.update(sub.name.lower() for sub in _subdirs(nested))
    # the essential part of the one starter list (osc.STARTER_KIT), minus what depends on
    # the console type - which a drive check doesn't know
    missing = [i for i in starter_kit("", "essential") if i.folder.lower() not in present]
    if missing:
        out.append(Finding("missing_apps", WARN if any(i.required for i in missing) else INFO,
                           f"Recommended apps not installed: {', '.join(i.title for i in missing)}.",
                           Fix.INSTALL_STARTER_APPS,
                           data={"missing": [i.folder.lower() for i in missing]}))
    return out


def _info(p: Path) -> tuple[Optional[DiscInfo], Optional[Finding]]:
    try:
        return disc_info(p), None
    except NeedsConversion as e:
        return None, Finding("needs_conversion", WARN,
                             f"{p.name} is in {e.fmt} format, which the Wii can't load. ReWii can "
                             "convert it.", Fix.CONVERT_GAME, p, {"format": e.fmt})
    except (DiscError, OSError, ValueError) as e:
        return None, Finding("unreadable_game", WARN, f"{p.name} doesn't look like a Wii or "
                             f"GameCube game ({e}).", None, p)


def _convert_finding(p: Path) -> Optional[Finding]:
    name = p.name.lower()
    if name.endswith(CONVERT_EXTS) or ".nkit." in name:
        fmt = "NKit" if ".nkit" in name else p.suffix[1:].upper()
        return Finding("needs_conversion", WARN,
                       f"{p.name} is in {fmt} format, which the Wii can't load. ReWii can "
                       "convert it.", Fix.CONVERT_GAME, p, {"format": fmt})
    if name.endswith(ARCHIVE_EXTS):
        return Finding("archive_on_drive", WARN,
                       f"{p.name} is a compressed archive. Extract it (or add the game with "
                       "ReWii) so the Wii can load it.", None, p)
    return None


def _big(p: Path, check_size: bool) -> bool:
    try:
        return check_size and p.stat().st_size > FAT32_MAX_FILE
    except OSError:
        return False


def check_wii_games(layout: DriveLayout, fat32_target: bool = True) -> list[Finding]:
    """Wii games must be ``wbfs/Title [ID]/ID.wbfs`` (+ ``ID.wbf1``... when split)."""
    out: list[Finding] = []
    root = layout.wbfs
    if not root.is_dir():
        return out

    def game(p: Path, folder: Optional[Path]) -> None:
        conv = _convert_finding(p)
        if conv:
            out.append(conv)
            return
        if not p.name.lower().endswith(WII_EXTS):
            return
        info, bad = _info(p)
        if bad:
            out.append(bad)
            return
        assert info is not None
        if not info.is_wii:
            out.append(Finding("gc_in_wbfs", WARN,
                               f"{p.name} is a GameCube game but it's in the Wii games folder. "
                               "ReWii can move it to games/.", Fix.MOVE_GC_GAME, p,
                               {"game_id": info.game_id}))
            return
        gid = info.game_id
        ext = p.suffix.lower()
        if folder is None:
            out.append(Finding("wii_flat", WARN,
                               f"{p.name} should be in its own folder "
                               f"(wbfs/{game_folder_name(info.title, gid)}/{gid}{ext}).",
                               Fix.MOVE_WII_GAME, p, {"game_id": gid}))
        elif id_from_folder(folder.name) != gid:
            out.append(Finding("wii_folder_name", WARN,
                               f"The folder \"{folder.name}\" should be named "
                               f"\"{game_folder_name(info.title, gid)}\" so loaders find the game.",
                               Fix.MOVE_WII_GAME, p, {"game_id": gid}))
        elif p.name != f"{gid}{ext}":
            out.append(Finding("wii_file_name", WARN,
                               f"{folder.name}/{p.name} should be named {gid}{ext}.",
                               Fix.MOVE_WII_GAME, p, {"game_id": gid}))
        if _big(p, fat32_target):
            if ext == ".wbfs":
                out.append(Finding("file_too_big", ERROR,
                                   f"{p.name} is larger than 4 GB, which FAT32 can't store. "
                                   "ReWii can split it into .wbfs + .wbf1 parts.",
                                   Fix.SPLIT_FILE, p))
            else:
                out.append(Finding("file_too_big", ERROR,
                                   f"{p.name} is larger than 4 GB, which FAT32 can't store. Re-add "
                                   "the game with ReWii to store it as split WBFS.",
                                   Fix.CONVERT_GAME, p))

    def orphans(files: list[Path]) -> None:
        names = {f.name.lower() for f in files}
        for f in files:
            sfx = f.suffix.lower()
            if sfx.startswith(".wbf") and sfx[4:].isdigit():
                if f"{f.stem.lower()}.wbfs" not in names:
                    out.append(Finding("orphan_split", ERROR,
                                       f"{f.name} is part of a split game but the main "
                                       f"{f.stem}.wbfs file is missing. Re-add the game.", None, f))
                elif int(sfx[4:]) > 1 and f"{f.stem.lower()}.wbf{int(sfx[4:]) - 1}" not in names:
                    out.append(Finding("split_gap", ERROR,
                                       f"{f.stem}.wbf{int(sfx[4:]) - 1} is missing, so this game "
                                       "is incomplete. Re-add the game.", None, f))

    flat = _files(root)
    orphans(flat)
    for f in flat:
        game(f, None)
    for d in _subdirs(root):
        files = _files(d)
        orphans(files)
        for f in files:
            game(f, d)
    return out


def check_gc_games(layout: DriveLayout) -> list[Finding]:
    """GameCube games should be ``games/Title [ID]/game.iso`` (+ ``disc2.iso``)."""
    out: list[Finding] = []
    root = layout.gc_games
    if not root.is_dir():
        return out

    def game(p: Path, folder: Optional[Path]) -> None:
        conv = _convert_finding(p)
        if conv:
            out.append(conv)
            return
        if not p.name.lower().endswith(GC_EXTS + (".wbfs",)):
            return
        info, bad = _info(p)
        if bad:
            out.append(bad)
            return
        assert info is not None
        if info.is_wii:
            out.append(Finding("wii_in_games", WARN,
                               f"{p.name} is a Wii game but it's in the GameCube games folder. "
                               "ReWii can move it to wbfs/.", Fix.MOVE_WII_GAME, p,
                               {"game_id": info.game_id}))
            return
        gid = info.game_id
        want = ("game" if info.disc_number == 0 else f"disc{info.disc_number + 1}") + p.suffix.lower()
        if folder is None:
            out.append(Finding("gc_flat", INFO,
                               f"{p.name} works in Nintendont but USB Loader GX won't list it. "
                               f"It should be games/{game_folder_name(info.title, gid)}/{want}.",
                               Fix.MOVE_GC_GAME, p, {"game_id": gid}))
        elif id_from_folder(folder.name) != gid:
            out.append(Finding("gc_folder_name", WARN,
                               f"The folder \"{folder.name}\" should be named "
                               f"\"{game_folder_name(info.title, gid)}\".",
                               Fix.MOVE_GC_GAME, p, {"game_id": gid}))
        elif p.name != want:
            kind = "gc_disc2_name" if info.disc_number else "gc_file_name"
            out.append(Finding(kind, WARN,
                               f"{folder.name}/{p.name} should be named {want}"
                               + (" (disc 2 of this game)." if info.disc_number else "."),
                               Fix.MOVE_GC_GAME, p, {"game_id": gid}))

    for f in _files(root):
        game(f, None)
    for d in _subdirs(root):
        for f in _files(d):
            game(f, d)
    return out


def _walk_junk(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for dn in dirnames:
            if dn in MAC_JUNK_DIRS:
                yield Path(dirpath) / dn
            else:
                keep.append(dn)
        dirnames[:] = keep
        for fn in filenames:
            if fn.startswith("._") or fn in MAC_JUNK_FILES:
                yield Path(dirpath) / fn


def check_mac_junk(layout: DriveLayout) -> list[Finding]:
    """macOS leaves ``._*`` files and index folders that clutter loaders' game/app lists."""
    junk = list(_walk_junk(layout.root))
    if not junk:
        return []
    return [Finding("mac_junk", INFO if len(junk) < 5 else WARN,
                    f"Found {len(junk)} hidden macOS file(s) (like ._ files and .Spotlight). They "
                    "can show up as broken entries on the Wii. ReWii can delete them.",
                    Fix.DELETE_MAC_JUNK, layout.root, {"paths": [str(p) for p in junk]})]


#: Top-level folders ReWii writes to; a generic ``*.part`` elsewhere may be the user's own file.
_MANAGED_TOPS = MANAGED_TOPS
_OWN_SUFFIXES = (PART_SUFFIX, NEW_SUFFIX, ".rewii-tmp")


def _is_leftover(rel: Path) -> bool:
    """Is this drive-relative file one of ReWii's unfinished files that is safe to delete?"""
    low = rel.name.lower()
    if low.startswith(TEMP_PREFIX) or low.endswith(_OWN_SUFFIXES):
        return True
    if low.endswith((".part", ".partial")):
        return len(rel.parts) == 1 or rel.parts[0].lower() in _MANAGED_TOPS
    return False


def _walk_temp(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in MAC_JUNK_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if _is_leftover(p.relative_to(root)):
                yield p


def _idle(p: Path, now: Optional[float] = None) -> bool:
    """Nothing has written to ``p`` for an hour, so no running copy owns it."""
    try:
        return (now or time.time()) - p.stat().st_mtime > TEMP_IDLE_SECONDS
    except OSError:
        return False


def check_temp_files(layout: DriveLayout) -> list[Finding]:
    """Unfinished files left by an interrupted copy (drive unplugged, crash, full drive)."""
    found = list(_walk_temp(layout.root))
    if not found:
        return []
    old = [p for p in found if _idle(p)]
    size = 0
    for p in found:
        try:
            size += p.stat().st_size
        except OSError:
            pass
    msg = (f"Found {len(found)} unfinished file(s) left behind by an interrupted copy "
           f"({human_size(size)}). They aren't usable and only take up space.")
    if old:
        msg += " ReWii can delete them."
    if len(old) < len(found):
        msg += (f" {len(found) - len(old)} of them changed less than an hour ago and may belong to "
                "a copy that is still running, so those are left alone for now - check again later.")
    return [Finding("temp_leftovers", WARN if old else INFO, msg,
                    Fix.DELETE_TEMP_FILES if old else None, layout.root,
                    {"paths": [str(p) for p in old], "recent": len(found) - len(old)})]


def check_writable(layout: DriveLayout) -> list[Finding]:
    """A drive mounted read-only (lock switch, or the computer found errors on it)."""
    ro = False
    try:
        if hasattr(os, "statvfs"):
            ro = bool(os.statvfs(layout.root).f_flag & getattr(os, "ST_RDONLY", 1))
        if not ro and layout.root.is_dir():
            ro = not os.access(layout.root, os.W_OK)
    except OSError:
        return []
    if not ro:
        return []
    return [Finding("read_only", ERROR,
                    "This drive is write-protected (read-only), so ReWii can't change anything on "
                    "it. On an SD card, check the lock switch. Otherwise the computer may have "
                    "found errors on the drive: eject it, plug it in again, and if that doesn't "
                    "help, repair it with the computer's disk tool or reformat it.", None, layout.root)]


def check(layout: DriveLayout, drive: Optional[Drive] = None) -> list[Finding]:
    """Run every check. ``drive`` (if known) enables filesystem/partition checks.

    Results are sorted errors first.
    """
    out: list[Finding] = []
    fat32_target = True
    out += check_writable(layout)
    if drive is not None:
        out += check_drive(drive)
        fat32_target = drive.filesystem not in (NTFS, EXFAT)
    out += check_apps(layout)
    out += check_wii_games(layout, fat32_target)
    out += check_gc_games(layout)
    out += check_mac_junk(layout)
    out += check_temp_files(layout)
    return sorted(out, key=lambda f: f.severity.rank)


# --------------------------------------------------------------------------- fixes
def _merge_move(src: Path, dst: Path, done: list[str], skipped: list[str]) -> None:
    """Move the contents of directory ``src`` into ``dst``; never overwrite existing files."""
    dst.mkdir(parents=True, exist_ok=True)
    for child in sorted(src.iterdir()):
        target = dst / child.name
        if child.is_dir() and target.is_dir():
            _merge_move(child, target, done, skipped)
        elif target.exists():
            skipped.append(str(child))
        else:
            shutil.move(str(child), str(target))
            done.append(f"Moved {child} -> {target}")
    _rmdir_if_empty(src)


def _is_game_folder(layout: DriveLayout, d: Path) -> bool:
    return d.parent in (layout.wbfs, layout.gc_games)


def _rmdir_if_empty(d: Path) -> None:
    try:
        d.rmdir()
    except OSError:
        pass


def _move_game(src: Path, dest: Path, rep: Reporter) -> list[str]:
    parts = split_parts(src)
    if src.suffix.lower() == ".wbfs":
        targets = [dest] + [dest.with_suffix(f".wbf{i}") for i in range(1, len(parts))]
    else:
        targets = [dest]
        parts = parts[:1]
    if fileops.same_spelling(src, dest):
        return []
    for t in targets:
        # a name that differs only in case is the same file on FAT32 - that's a rename, not a clash
        if t.exists() and not any(same_file(t, p) for p in parts):
            raise FixError(f"{t} already exists; not overwriting it.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    done = []
    old_parent = src.parent
    for p, t in zip(parts, targets):
        rep.check()
        if not fileops.same_spelling(p, t):
            rename_path(p, t)                  # handles case-only renames on FAT32
            done.append(f"Moved {p} -> {t}")
    if old_parent != dest.parent:
        _rmdir_if_empty(old_parent)
    return done


def _game_dest(layout: DriveLayout, src: Path, info: DiscInfo) -> Path:
    ext = src.suffix.lower()
    if info.is_wii:
        folder = src.parent
        if folder.parent == layout.wbfs and id_from_folder(folder.name) == info.game_id:
            return folder / f"{info.game_id}{ext}"
        return layout.wii_game_file(info.title, info.game_id, ext)
    folder = src.parent
    name = ("game" if info.disc_number == 0 else f"disc{info.disc_number + 1}") + ext
    if folder.parent == layout.gc_games and id_from_folder(folder.name) == info.game_id:
        return folder / name
    # second discs join the first disc's folder if it already exists under another title
    if layout.gc_games.is_dir():
        for d in _subdirs(layout.gc_games):
            if id_from_folder(d.name) == info.game_id:
                return d / name
    return layout.gc_game_file(info.title, info.game_id, info.disc_number, ext)


def fix(layout: DriveLayout, finding: Finding, progress: Optional[ProgressFn] = None,
        cancel: Optional[CancelToken] = None) -> list[str]:
    """Apply the safe fix for ``finding`` and return a log of what was done.

    Raises :class:`FixError` if the finding has no safe fix or the fix would
    overwrite something. Never deletes user data except macOS junk files.
    """
    rep = Reporter(progress, cancel)
    fixer = FIXERS.get(finding.fix_id or "")
    if finding.fix_id not in SAFE_FIXES or fixer is None:
        raise FixError(f"'{finding.id}' can't be fixed automatically.")
    layout.require_root()
    p = finding.path
    if p is not None and not is_inside(Path(p), layout.root):
        # a finding from another drive (the drive was switched after the check)
        raise FixError("That finding belongs to a different drive. Check the drive again.")
    if p is None and finding.fix_id not in _WHOLE_DRIVE_FIXES:
        raise FixError("This finding doesn't say which file to fix.")
    return fixer(layout, finding, rep)


def _fix_mac_junk(layout: DriveLayout, finding: Finding, rep: Reporter) -> list[str]:
    paths = [Path(x) for x in finding.data.get("paths", [])] or list(_walk_junk(layout.root))
    rep.set_total(len(paths), "Deleting macOS junk files", unit=UNIT_STEPS)
    deleted: list[str] = []
    root = layout.root.resolve()
    for jp in paths:
        rep.check()
        try:
            if root in jp.parent.resolve().parents or jp.parent.resolve() == root:
                if jp.is_dir() and not jp.is_symlink():
                    shutil.rmtree(jp)
                elif jp.exists() or jp.is_symlink():
                    jp.unlink()
                deleted.append(f"Deleted {jp}")
        except OSError as e:
            deleted.append(f"{NOT_FIXED} {jp.name}: {explain_os_error(e, layout.root)}")
        rep.advance(1)
    return deleted


def _fix_temp_files(layout: DriveLayout, finding: Finding, rep: Reporter) -> list[str]:
    # look again instead of trusting the finding: only ReWii's own temp names, only files,
    # and only ones nothing has written to for an hour (a running copy is never touched)
    rep.note("Deleting unfinished files")
    removed: list[str] = []
    for tp in _walk_temp(layout.root):
        rep.check()
        if tp.is_symlink() or not tp.is_file() or not _idle(tp):
            continue
        try:
            tp.unlink()
            removed.append(f"Deleted {tp}")
            if _is_game_folder(layout, tp.parent):      # "Title [ID]" with nothing else in it
                _rmdir_if_empty(tp.parent)
        except OSError as e:
            removed.append(f"{NOT_FIXED} {tp.name}: {explain_os_error(e, layout.root)}")
    return removed


def _fix_nested_apps(layout: DriveLayout, finding: Finding, rep: Reporter) -> list[str]:
    p = finding.path
    assert p is not None
    rep.note("Moving apps to the right folder")
    done: list[str] = []
    skipped: list[str] = []
    _merge_move(p, layout.apps, done, skipped)
    parent = p.parent
    if parent != layout.apps and parent.parent == layout.apps:
        _rmdir_if_empty(parent)
    if skipped:
        done.append("Kept (already present in apps/): " + ", ".join(skipped))
    return done


def _fix_app_folder(layout: DriveLayout, finding: Finding, rep: Reporter) -> list[str]:
    p = finding.path
    assert p is not None
    rep.note("Moving app up one folder")
    done: list[str] = []
    skipped: list[str] = []
    outer = p.parent
    dest = layout.apps / p.name
    if dest.resolve() == outer.resolve():
        tmp = outer.with_name(outer.name + ".rewii-tmp")
        os.replace(p, tmp)
        _merge_move(tmp, outer, done, skipped)
    else:
        _merge_move(p, dest, done, skipped)
        _rmdir_if_empty(outer)
    if skipped:
        done.append("Kept (already present): " + ", ".join(skipped))
    return done


def _fix_move_game(layout: DriveLayout, finding: Finding, rep: Reporter) -> list[str]:
    p = finding.path
    assert p is not None
    try:
        info = disc_info(p)
    except (DiscError, OSError) as e:
        raise FixError(f"Couldn't read {p.name}: {e}") from e
    rep.note(f"Moving {info.title}")
    return _move_game(p, _game_dest(layout, p, info), rep)


def _fix_split_file(layout: DriveLayout, finding: Finding, rep: Reporter) -> list[str]:
    p = finding.path
    assert p is not None
    if p.suffix.lower() != ".wbfs":
        raise FixError("Only .wbfs files can be split; re-add other games with ReWii.")
    parts = split_file(p, progress=rep.fn, cancel=rep.cancel)
    return [f"Split {p.name} into {len(parts)} parts"]


#: What :func:`fix` runs for each safe fix (the keys are exactly :data:`SAFE_FIXES`).
FIXERS: dict[str, Callable[[DriveLayout, Finding, Reporter], list[str]]] = {
    Fix.DELETE_MAC_JUNK: _fix_mac_junk,
    Fix.DELETE_TEMP_FILES: _fix_temp_files,
    Fix.FLATTEN_NESTED_APPS: _fix_nested_apps,
    Fix.FLATTEN_APP_FOLDER: _fix_app_folder,
    Fix.MOVE_WII_GAME: _fix_move_game,
    Fix.MOVE_GC_GAME: _fix_move_game,
    Fix.SPLIT_FILE: _fix_split_file,
}
#: Fixes that look at the whole drive instead of the one path in the finding.
_WHOLE_DRIVE_FIXES = frozenset({Fix.DELETE_MAC_JUNK, Fix.DELETE_TEMP_FILES})


def split_file(path: Path | str, part_size: int = FAT32_SPLIT,
               progress: Optional[ProgressFn] = None,
               cancel: Optional[CancelToken] = None) -> list[Path]:
    """Split a ``.wbfs`` file in place into ``X.wbfs`` + ``X.wbf1`` + ... of ``part_size`` bytes.

    Parts are written from the end and the original truncated after each, so
    only one part's worth of extra space is needed. Plain byte split, the same
    as Wii Backup Manager/wit (the WBFS header in part 0 describes the whole
    image). Returns all part paths. If cancelled or on error, finished parts are
    joined back so the original file is left whole.
    """
    path = Path(path)
    if path.suffix.lower() != ".wbfs":
        raise FixError("split_file only supports .wbfs files (.wbf1 naming)")
    if part_size <= 0 or part_size % 512:
        raise FixError("part_size must be a positive multiple of 512")
    size = path.stat().st_size
    nparts = max(1, -(-size // part_size))
    existing = [path.with_suffix(f".wbf{i}") for i in range(1, nparts)]
    if any(e.exists() for e in existing):
        raise FileExistsError("split parts already exist next to " + path.name)
    rep = Reporter(progress, cancel, total=size - min(size, part_size), message="Splitting")
    chunk = 8 * 1024 * 1024
    written: list[Path] = []          # highest part first
    with open(path, "r+b") as src:
        try:
            for i in range(nparts - 1, 0, -1):
                rep.check()
                start = i * part_size
                end = min(size, start + part_size)
                dst_path = path.with_suffix(f".wbf{i}")
                tmp = part_path(dst_path)
                try:
                    with open(tmp, "wb") as dst:
                        src.seek(start)
                        left = end - start
                        while left:
                            buf = src.read(min(chunk, left))
                            if not buf:
                                raise OSError("unexpected end of file")
                            dst.write(buf)
                            left -= len(buf)
                            rep.advance(len(buf))
                        dst.flush()
                        os.fsync(dst.fileno())     # the part must be on the drive before we truncate
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise
                os.replace(tmp, dst_path)
                written.append(dst_path)
                src.truncate(start)
                size = start
        except BaseException:
            # roll back: append the finished parts (lowest first) back onto the original
            src.seek(0, os.SEEK_END)
            for part in reversed(written):
                with open(part, "rb") as pf:
                    shutil.copyfileobj(pf, src, chunk)
                part.unlink()
            raise
    return [path] + [path.with_suffix(f".wbf{i}") for i in range(1, nparts)]
