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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from collections.abc import Iterator

from .disc import FAT32_SPLIT, DiscError, DiscInfo, NeedsConversion, disc_info, split_parts
from .drives import EXFAT, FAT16, FAT32, NTFS, UNKNOWN, WBFS, Drive, human_size
from .fat32 import MAX_SIZE
from .layout import DriveLayout, game_folder_name, id_from_folder
from .progress import CancelToken, ProgressFn, Reporter

INFO, WARN, ERROR = "info", "warn", "error"

FAT32_MAX_FILE = 0xFFFFFFFF          # 4 GiB - 1
RECOMMENDED_CLUSTER = 32768

SAFE_FIXES = frozenset({"flatten_nested_apps", "flatten_app_folder", "move_wii_game",
                        "move_gc_game", "split_file", "delete_mac_junk"})

#: Starter apps: folder name under apps/ -> (display name, essential?)
STARTER_APPS: dict[str, tuple[str, bool]] = {
    "usbloader_gx": ("USB Loader GX", True),
    "nintendont": ("Nintendont", True),
    "yawmme": ("YAWMME", False),
    "savegame_manager_gx": ("SaveGame Manager GX", False),
    "wiixplorer": ("WiiXplorer", False),
}

MAC_JUNK_DIRS = frozenset({".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems"})
MAC_JUNK_FILES = frozenset({".DS_Store"})

WII_EXTS = (".wbfs", ".iso", ".ciso")
GC_EXTS = (".iso", ".gcm", ".ciso")
CONVERT_EXTS = (".rvz", ".wia", ".gcz", ".wdf", ".nkit")
ARCHIVE_EXTS = (".zip", ".7z", ".rar")


class FixError(Exception):
    """A fix couldn't be applied (conflict, unsupported fix, unreadable game...)."""


@dataclass
class Finding:
    """One problem (or note) about a drive."""

    id: str                       # machine-readable kind, e.g. "nested_apps"
    severity: str                 # INFO | WARN | ERROR
    message: str                  # plain language for the user
    fix_id: Optional[str] = None  # see module docstring / SAFE_FIXES
    path: Optional[Path] = None   # the file/folder concerned, if any
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def auto_fixable(self) -> bool:
        """True if :func:`fix` can repair this finding by itself."""
        return self.fix_id in SAFE_FIXES


# --------------------------------------------------------------------------- drive-level
def check_drive(drive: Drive) -> list[Finding]:
    """Checks that only need the Drive: filesystem, partition style, clusters, size, free space."""
    out: list[Finding] = []
    fs = drive.filesystem
    if fs == NTFS:
        out.append(Finding("fs_ntfs", WARN,
                           "This drive uses NTFS. USB Loader GX can play Wii games from NTFS, but "
                           "the Homebrew Channel, Nintendont (GameCube games) and most apps need "
                           "FAT32. Reformatting to FAT32 is recommended.", "format_drive"))
    elif fs == EXFAT:
        out.append(Finding("fs_exfat", ERROR,
                           "This drive uses exFAT, which the Wii can't read. It needs to be "
                           "reformatted to FAT32.", "format_drive"))
    elif fs == WBFS:
        out.append(Finding("fs_wbfs", ERROR,
                           "This is an old WBFS-formatted drive. Modern loaders use FAT32. Import "
                           "your games to the computer first, then reformat the drive.", "import_wbfs"))
    elif fs == FAT16:
        out.append(Finding("fs_fat16", ERROR,
                           "This drive uses FAT16, which is too small for Wii games. It needs to "
                           "be reformatted to FAT32.", "format_drive"))
    elif fs != FAT32:
        what = "no file system" if fs == UNKNOWN else fs
        extra = " (ReWii needs admin rights to look closer)" if drive.needs_admin else ""
        out.append(Finding("fs_other", ERROR,
                           f"This drive has {what}{extra}, which the Wii can't read. It needs to "
                           "be formatted as FAT32.", "format_drive"))
    if drive.partition_style == "GPT":
        out.append(Finding("gpt", ERROR,
                           "This drive uses the GPT partition style. The Wii needs MBR. "
                           "Reformatting with ReWii fixes this.", "format_drive"))
    cs = drive.cluster_size
    if fs == FAT32 and cs and cs != RECOMMENDED_CLUSTER:
        out.append(Finding("cluster_size", WARN,
                           f"This drive uses {cs // 1024 or cs} {'KB' if cs >= 1024 else 'byte'} "
                           "clusters. 32 KB clusters are recommended for the Wii (faster loading, "
                           "fewer problems). Reformatting fixes this.", "format_drive",
                           data={"cluster_size": cs}))
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
        return sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []


def _files(d: Path) -> list[Path]:
    try:
        return sorted(p for p in d.iterdir() if p.is_file() and not p.name.startswith("._"))
    except OSError:
        return []


def check_apps(layout: DriveLayout) -> list[Finding]:
    """Homebrew checks: nested apps folders, apps without boot.dol, missing starter apps."""
    out: list[Finding] = []
    apps = layout.apps
    if not apps.is_dir():
        out.append(Finding("no_apps", WARN,
                           "There's no apps folder, so the Homebrew Channel won't find any apps "
                           "on this drive.", "install_starter_apps"))
        return out
    nested = _ci_child(apps, "apps")
    if nested is not None and nested.is_dir():
        out.append(Finding("nested_apps", ERROR,
                           "There's an apps folder inside the apps folder (apps/apps), so the "
                           "Homebrew Channel can't see those apps. ReWii can move them up.",
                           "flatten_nested_apps", nested))
    present = set()
    data_only = {"images", "cache_bnr", "cache", "theme", "language", "gxglobal.cfg", "gxgamesettings.cfg"}
    for app in _subdirs(apps):
        if app is nested:
            continue
        if _has_boot(app):
            present.add(app.name.lower())
            continue
        # loader data (covers, banner cache, settings) saved before the app itself is installed
        try:
            names = {c.name.lower() for c in app.iterdir()}
        except OSError:
            names = set()
        if names and names <= data_only:
            continue
        present.add(app.name.lower())
        inner_apps = _ci_child(app, "apps")
        if inner_apps is not None and inner_apps.is_dir():
            out.append(Finding("nested_apps", ERROR,
                               f"apps/{app.name} contains another apps folder; the app inside "
                               "won't show up. ReWii can move it to the right place.",
                               "flatten_nested_apps", inner_apps))
            continue
        inner = [s for s in _subdirs(app) if _has_boot(s)]
        if len(inner) == 1:
            out.append(Finding("app_double_folder", WARN,
                               f"apps/{app.name} has its app one folder too deep "
                               f"(apps/{app.name}/{inner[0].name}). ReWii can move it up.",
                               "flatten_app_folder", inner[0]))
            present.add(inner[0].name.lower())
            continue
        out.append(Finding("app_no_boot", WARN,
                           f"apps/{app.name} has no boot.dol or boot.elf, so it won't start. "
                           "Reinstall the app.", None, app))
    if nested is not None and nested.is_dir():
        present.update(s.name.lower() for s in _subdirs(nested))
    missing = [(k, name, ess) for k, (name, ess) in STARTER_APPS.items() if k not in present]
    if missing:
        essential = any(ess for _k, _n, ess in missing)
        names = ", ".join(n for _k, n, _e in missing)
        out.append(Finding("missing_apps", WARN if essential else INFO,
                           f"Recommended apps not installed: {names}.", "install_starter_apps",
                           data={"missing": [k for k, _n, _e in missing]}))
    return out


def _info(p: Path) -> tuple[Optional[DiscInfo], Optional[Finding]]:
    try:
        return disc_info(p), None
    except NeedsConversion as e:
        return None, Finding("needs_conversion", WARN,
                             f"{p.name} is in {e.fmt} format, which the Wii can't load. ReWii can "
                             "convert it.", "convert_game", p, {"format": e.fmt})
    except (DiscError, OSError, ValueError) as e:
        return None, Finding("unreadable_game", WARN, f"{p.name} doesn't look like a Wii or "
                             f"GameCube game ({e}).", None, p)


def _convert_finding(p: Path) -> Optional[Finding]:
    name = p.name.lower()
    if name.endswith(CONVERT_EXTS) or ".nkit." in name:
        fmt = "NKit" if ".nkit" in name else p.suffix[1:].upper()
        return Finding("needs_conversion", WARN,
                       f"{p.name} is in {fmt} format, which the Wii can't load. ReWii can "
                       "convert it.", "convert_game", p, {"format": fmt})
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
                               "ReWii can move it to games/.", "move_gc_game", p,
                               {"game_id": info.game_id}))
            return
        gid = info.game_id
        ext = p.suffix.lower()
        if folder is None:
            out.append(Finding("wii_flat", WARN,
                               f"{p.name} should be in its own folder "
                               f"(wbfs/{game_folder_name(info.title, gid)}/{gid}{ext}).",
                               "move_wii_game", p, {"game_id": gid}))
        elif id_from_folder(folder.name) != gid:
            out.append(Finding("wii_folder_name", WARN,
                               f"The folder \"{folder.name}\" should be named "
                               f"\"{game_folder_name(info.title, gid)}\" so loaders find the game.",
                               "move_wii_game", p, {"game_id": gid}))
        elif p.name != f"{gid}{p.suffix}":
            out.append(Finding("wii_file_name", WARN,
                               f"{folder.name}/{p.name} should be named {gid}{ext}.",
                               "move_wii_game", p, {"game_id": gid}))
        if _big(p, fat32_target):
            if ext == ".wbfs":
                out.append(Finding("file_too_big", ERROR,
                                   f"{p.name} is larger than 4 GB, which FAT32 can't store. "
                                   "ReWii can split it into .wbfs + .wbf1 parts.",
                                   "split_file", p))
            else:
                out.append(Finding("file_too_big", ERROR,
                                   f"{p.name} is larger than 4 GB, which FAT32 can't store. Re-add "
                                   "the game with ReWii to store it as split WBFS.",
                                   "convert_game", p))

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
                               "ReWii can move it to wbfs/.", "move_wii_game", p,
                               {"game_id": info.game_id}))
            return
        gid = info.game_id
        want = ("game" if info.disc_number == 0 else f"disc{info.disc_number + 1}") + p.suffix.lower()
        if folder is None:
            out.append(Finding("gc_flat", INFO,
                               f"{p.name} works in Nintendont but USB Loader GX won't list it. "
                               f"It should be games/{game_folder_name(info.title, gid)}/{want}.",
                               "move_gc_game", p, {"game_id": gid}))
        elif id_from_folder(folder.name) != gid:
            out.append(Finding("gc_folder_name", WARN,
                               f"The folder \"{folder.name}\" should be named "
                               f"\"{game_folder_name(info.title, gid)}\".",
                               "move_gc_game", p, {"game_id": gid}))
        elif p.name.lower() != want:
            kind = "gc_disc2_name" if info.disc_number else "gc_file_name"
            out.append(Finding(kind, WARN,
                               f"{folder.name}/{p.name} should be named {want}"
                               + (" (disc 2 of this game)." if info.disc_number else "."),
                               "move_gc_game", p, {"game_id": gid}))

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
                    "delete_mac_junk", layout.root, {"paths": [str(p) for p in junk]})]


def check(layout: DriveLayout, drive: Optional[Drive] = None) -> list[Finding]:
    """Run every check. ``drive`` (if known) enables filesystem/partition checks.

    Results are sorted errors first.
    """
    out: list[Finding] = []
    fat32_target = True
    if drive is not None:
        out += check_drive(drive)
        fat32_target = drive.filesystem not in (NTFS, EXFAT)
    out += check_apps(layout)
    out += check_wii_games(layout, fat32_target)
    out += check_gc_games(layout)
    out += check_mac_junk(layout)
    order = {ERROR: 0, WARN: 1, INFO: 2}
    return sorted(out, key=lambda f: order.get(f.severity, 3))


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
    if src.resolve() == dest.resolve():
        return []
    for t in targets:
        if t.exists() and t.resolve() not in {p.resolve() for p in parts}:
            raise FixError(f"{t} already exists; not overwriting it.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    done = []
    old_parent = src.parent
    for p, t in zip(parts, targets):
        rep.check()
        if p.parent.resolve() == t.parent.resolve() and p.name.lower() == t.name.lower():
            tmp = p.with_name(p.name + ".rewii-tmp")      # case-only rename on case-insensitive FS
            os.replace(p, tmp)
            os.replace(tmp, t)
        else:
            shutil.move(str(p), str(t))
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
    fid = finding.fix_id
    if fid not in SAFE_FIXES:
        raise FixError(f"'{finding.id}' can't be fixed automatically.")
    p = finding.path

    if fid == "delete_mac_junk":
        paths = [Path(x) for x in finding.data.get("paths", [])] or list(_walk_junk(layout.root))
        rep.set_total(len(paths), "Deleting macOS junk files")
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
            except OSError:
                pass
            rep.advance(1)
        return deleted

    if p is None:
        raise FixError("This finding doesn't say which file to fix.")

    if fid == "flatten_nested_apps":
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

    if fid == "flatten_app_folder":
        rep.note("Moving app up one folder")
        done, skipped = [], []
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

    if fid in ("move_wii_game", "move_gc_game"):
        try:
            info = disc_info(p)
        except (DiscError, OSError) as e:
            raise FixError(f"Couldn't read {p.name}: {e}") from e
        rep.note(f"Moving {info.title}")
        return _move_game(p, _game_dest(layout, p, info), rep)

    # split_file
    if p.suffix.lower() != ".wbfs":
        raise FixError("Only .wbfs files can be split; re-add other games with ReWii.")
    parts = split_file(p, progress=progress, cancel=cancel)
    return [f"Split {p.name} into {len(parts)} parts"]


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
        raise ValueError("split_file only supports .wbfs files (.wbf1 naming)")
    if part_size <= 0 or part_size % 512:
        raise ValueError("part_size must be a positive multiple of 512")
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
                tmp = dst_path.with_name(dst_path.name + ".partial")
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
