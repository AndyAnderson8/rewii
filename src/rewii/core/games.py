"""Wii and GameCube game management: the PC library and the Wii's drive.

Library (on the PC):   <library>/Games/Wii/Title [ID].wbfs, <library>/Games/GameCube/Title [ID]/game.iso
Drive (USB):           wbfs/Title [ID]/ID.wbfs (+.wbf1...), games/Title [ID]/game.iso (+disc2.iso)
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from collections.abc import Callable, Iterable, Iterator

from . import config as _config
from .config import Settings
from .disc import (FAT32_SPLIT, GC_DRIVE_EXTS, INCOMPLETE, LIBRARY_EXTS, WII_DRIVE_EXTS, cleanrip_part, NEEDS_CONVERSION, NEEDS_CONVERSION_EXTS, WII_DL_SECTORS,
                   WII_SECTOR, DiscError, aes_cbc_decrypt, incomplete_reason,
                   open_disc, parse_header, region_of, sniff_format, split_parts, used_sectors,
                   wii_partitions, wii_usage_table)
from . import fileops
from .fileops import (TEMP_PREFIX as TMP_PREFIX, DriveNotConnected, explain_os_error, is_temp_name,
                      remove_quietly, rename_path, same_file, same_spelling)
from .ids import is_disc_id, platform_guess
from .layout import DriveLayout, game_folder_name, id_from_folder
from .progress import UNIT_STEPS, CancelToken, ProgressFn, Reporter
from .wbfs import WbfsPartition, copy_file, write_iso, write_wbfs
from .errors import ReWiiError

#: Formats that must be converted before a console can use them (defined in ``disc``).
CONVERTIBLE = NEEDS_CONVERSION
CONVERTIBLE_EXTS = NEEDS_CONVERSION_EXTS
DRIVE_WII_EXTS = WII_DRIVE_EXTS
DRIVE_GC_EXTS = GC_DRIVE_EXTS
UNKNOWN_ID = "??????"             # a game that needs conversion and has no [ID] in its name
STALE_TMP_AGE = 24 * 3600
WORK_DIR_NAME = "work"             # under the cache folder: joined / converted images

#: Values of ``Game.problem`` ("" = fine). They are also what the lists show.
NEEDS_CONVERSION_PROBLEM = "needs conversion"
MISPLACED = "misplaced"
INCOMPLETE_PROBLEM = "incomplete"   # a split part is missing, or the file is cut short

TitleLookup = Callable[[str], Optional[str]]


class GameError(ReWiiError):
    pass


@dataclass
class Game:
    game_id: str
    title: str                 # best known title (GameTDB if available)
    disc_title: str            # title from the disc header
    platform: str              # "wii" | "gc"
    disc_number: int
    region: str
    fmt: str                   # ISO/WBFS/CISO/RVZ/...
    path: Path                 # main file
    files: list[Path] = field(default_factory=list)
    size: int = 0
    location: str = "library"  # "library" | "drive"
    problem: str = ""          # NEEDS_CONVERSION_PROBLEM, MISPLACED or INCOMPLETE_PROBLEM

    @property
    def is_wii(self) -> bool:
        return self.platform == "wii"

    @property
    def needs_conversion(self) -> bool:
        return self.fmt in CONVERTIBLE

    @property
    def key(self) -> str:
        return f"{self.game_id}:{self.disc_number}"


def _title(lookup: TitleLookup | None, gid: str, fallback: str) -> str:
    if lookup:
        try:
            t = lookup(gid)
            if t:
                return t
        except Exception:
            pass
    return fallback


def identify(path: Path, lookup: TitleLookup | None = None, location: str = "library") -> Optional[Game]:
    """Read a game file's header. Returns None if it isn't a disc image."""
    path = Path(path)
    try:
        fmt = sniff_format(path)
    except OSError:
        return None
    files = split_parts(path)
    try:
        size = sum(p.stat().st_size for p in files)
    except OSError:
        return None
    if fmt in CONVERTIBLE:
        gid = id_from_folder(path.stem) or id_from_folder(path.parent.name) or UNKNOWN_ID
        plat = platform_guess(gid)
        t = _title(lookup, gid, path.stem)
        return Game(gid, t, path.stem, plat, 0, region_of(gid), fmt, path, files, size, location,
                    NEEDS_CONVERSION_PROBLEM)
    if fmt not in ("ISO", "WBFS", "CISO"):
        return None
    problem = ""
    try:
        with open_disc(path) as d:
            gid, dt, plat, num, _ver = parse_header(d.read(0, 0x60))
            # a split game whose .wbfN part is gone: the block table points past the data there is
            if fmt == "WBFS" and (d.missing(0, d.size) or _part_gap(path, files)):
                problem = INCOMPLETE_PROBLEM
    except (DiscError, OSError):       # includes damaged headers and invalid game IDs
        return None
    return Game(gid, _title(lookup, gid, dt), dt, plat, num, region_of(gid), fmt, path, files, size,
                location, problem)


def _part_gap(path: Path, files: list[Path]) -> bool:
    """True if a later ``.wbfN`` part exists beyond the last consecutive one."""
    try:
        have = {f.name.lower() for f in files}
        stem = path.stem.lower()
        return any(x.name.lower() not in have and x.stem.lower() == stem
                   and re.fullmatch(r"\.wbf\d+", x.suffix.lower()) for x in path.parent.iterdir())
    except OSError:
        return False


def _iter_images(root: Path, exts: Iterable[str]) -> Iterable[Path]:
    """Image files under ``root``. ReWii's own staging and temp files are never
    listed. Read-only: leftovers of a crash are removed by :func:`purge_stale_temp`,
    not here."""
    if not root.exists():
        return
    exts = set(exts)
    for p in sorted(root.rglob("*")):
        if is_temp_name(p.name):
            continue
        if p.is_file() and p.suffix.lower() in exts and not p.name.startswith("._"):
            yield p


def _is_stale(p: Path) -> bool:
    try:
        return time.time() - p.stat().st_mtime > STALE_TMP_AGE
    except OSError:
        return False


_SWAP_SUFFIXES = (fileops.PARTIAL_DIR_SUFFIX, fileops.NEW_SUFFIX, fileops.OLD_SUFFIX)


def _swap_leftovers(base: Path) -> list[Path]:
    """Staging/swap leftovers under ``base``; a leftover folder isn't looked into."""
    out: list[Path] = []
    if not base.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(base):
        keep: list[str] = []
        for d in dirnames:
            if d.lower().endswith(_SWAP_SUFFIXES):
                out.append(Path(dirpath) / d)
            else:
                keep.append(d)
        dirnames[:] = keep
        out += [Path(dirpath) / f for f in filenames if f.lower().endswith(_SWAP_SUFFIXES)]
    return out


def purge_stale_temp(settings: Settings | None = None, layout: DriveLayout | None = None) -> list[Path]:
    """Delete what an interrupted copy or conversion left behind, and return what was removed.

    * game staging files (``.rewii-tmp-*``) in the library (``settings``) and in the
      drive's game folders (``layout``),
    * job folders in the cache's work folder,

    but only ones nothing has touched for a day - younger ones may belong to a copy
    that is still running. This is the only place they are cleaned up: listing games
    never deletes anything. The app calls it once at start, ``rewii health`` when run.
    """
    removed: list[Path] = []
    roots: list[Path] = []
    if settings is not None:
        roots += [settings.wii_games_dir, settings.gc_games_dir]
    if layout is not None and layout.connected():
        roots += [layout.wbfs, layout.gc_games]
    for root in roots:
        try:
            found = [p for p in root.rglob(TMP_PREFIX + "*") if p.is_file() and not p.is_symlink()]
        except OSError:
            continue
        for p in found:
            if _is_stale(p):
                try:
                    p.unlink()
                    removed.append(p)
                except OSError:
                    pass
    # library: snapshot/NAND staging folders, save and mod swaps (``*.partial``,
    # ``*.rewii-new``, ``*.rewii-old``). A ``*.rewii-old`` whose original is missing is
    # put back instead of deleted - it is the only copy left.
    if settings is not None:
        for base in (settings.saves_dir, settings.mods_dir, settings.drive_backups_dir,
                     settings.nand_backups_dir):
            for p in _swap_leftovers(base):
                if fileops.idle_for(p, STALE_TMP_AGE):
                    try:
                        if fileops.settle_leftover(p):
                            removed.append(p)
                    except OSError:
                        pass
    try:
        work = _config.cache_dir() / WORK_DIR_NAME
        jobs = list(work.iterdir()) if work.is_dir() else []
    except OSError:
        jobs = []
    for old in jobs:
        if _is_stale(old):
            try:
                shutil.rmtree(old) if old.is_dir() else old.unlink()
                removed.append(old)
            except OSError:
                pass
    return removed


def scan_library(settings: Settings, lookup: TitleLookup | None = None) -> list[Game]:
    out: list[Game] = []
    for root in (settings.wii_games_dir, settings.gc_games_dir):
        for p in _iter_images(root, LIBRARY_EXTS):
            g = identify(p, lookup, "library")
            if g:
                out.append(g)
    return sorted(out, key=lambda g: (g.platform != "wii", g.title.lower(), g.disc_number))


def scan_drive(layout: DriveLayout, lookup: TitleLookup | None = None) -> list[Game]:
    out: list[Game] = []
    for p in _iter_images(layout.wbfs, DRIVE_WII_EXTS | CONVERTIBLE_EXTS):
        g = identify(p, lookup, "drive")
        if not g:
            continue
        _folder_title(g, p)
        if p.parent.parent != layout.wbfs or id_from_folder(p.parent.name) != g.game_id \
                or p.name != f"{g.game_id}{p.suffix.lower()}":
            g.problem = g.problem or MISPLACED
        out.append(g)
    for p in _iter_images(layout.gc_games, DRIVE_GC_EXTS | CONVERTIBLE_EXTS):
        g = identify(p, lookup, "drive")
        if not g:
            continue
        _folder_title(g, p)
        # the same rules as health.check_gc_games, so both agree on what is misplaced
        want = ("game" if g.disc_number == 0 else f"disc{g.disc_number + 1}") + p.suffix.lower()
        if p.parent.parent != layout.gc_games or id_from_folder(p.parent.name) != g.game_id \
                or p.name != want:
            g.problem = g.problem or MISPLACED
        out.append(g)
    return sorted(out, key=lambda g: (g.platform != "wii", g.title.lower(), g.disc_number))


def _folder_title(g: Game, p: Path) -> None:
    """Prefer the "Title [ID]" folder name over the raw disc title when nothing better is known."""
    if g.title == g.disc_title and id_from_folder(p.parent.name) == g.game_id:
        t = p.parent.name.rsplit(" [", 1)[0].strip()
        if t:
            g.title = t


# --------------------------------------------------------------------------- helpers
def free_space(path: Path) -> int:
    p = Path(path)
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free


def estimate_install_size(path: Path, scrub: bool = True) -> int:
    """Bytes a game will take on the drive (a rough upper bound for images that
    need converting or can't be read)."""
    raw = sum(p.stat().st_size for p in split_parts(path))
    g_fmt = sniff_format(path)
    if g_fmt in CONVERTIBLE:
        from . import convert
        return convert.expected_iso_size(path, g_fmt) or raw * 3
    try:
        with open_disc(path) as d:
            _gid, _t, plat, *_ = parse_header(d.read(0, 0x60))
            if plat == "gc":
                return raw
            used = used_sectors(d, scrub=scrub)
            blocks = {i * WII_SECTOR // (2 << 20) for i, u in enumerate(used) if u}
            return (len(blocks) + 1) * (2 << 20)
    except DiscError:
        return raw




@contextlib.contextmanager
def _work_dir() -> Iterator[Path]:
    """A private folder under the app cache for joined / converted images.

    The system temp folder is often RAM-backed or small, and these files are
    1.4-8.5 GB. The folder is always removed again; ones left by a crash are
    cleared by :func:`purge_stale_temp`."""
    base = _config.cache_dir() / WORK_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix="job-", dir=base))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _need_work_space(folder: Path, need: int, what: str) -> None:
    if need and free_space(folder) < need + 64 * 1024 * 1024:
        raise GameError(f"Not enough free space on this computer to {what}: it needs "
                        f"{need / 1e9:.1f} GB of temporary space in {folder.parent}")


def _prepare_source(src: Path, tmpdir: Path, rep: Reporter) -> Path:
    """Join CleanRip part files / convert RVZ/NKit/etc. to ISO in ``tmpdir`` if needed."""
    from . import cleanrip, convert
    try:
        if cleanrip_part(src.name) is not None:
            dumps = cleanrip.find_dumps(src.parent)
            dump = next((d for d in dumps if any(p.name.lower() == src.name.lower() for p in d.parts)), None)
            if dump is None:
                raise GameError(f"{src.name} is one part of a split dump, but its first part "
                                "(.part0.iso) is missing")
            if not dump.complete:
                raise GameError(f"{dump.name}: dump parts {dump.missing_parts} are missing")
            problem = dump.size_problem()
            if problem:
                raise GameError(f"{dump.name}: {problem}")
            _need_work_space(tmpdir, dump.size, "join the dump")
            rep.note(f"Joining CleanRip dump {dump.name}")
            out = tmpdir / "joined.iso"
            # the redump DAT is only informational; the install doesn't use it
            res = cleanrip.join_dump(dump, out, rep.fn, rep.cancel, use_dat=False)
            if res.dumpinfo_match is False:
                raise GameError(f"{dump.name}: the dump is damaged ({', '.join(res.mismatches)}) - rip it again")
            return out
        fmt = sniff_format(src)
        if fmt not in CONVERTIBLE:
            return src
        _need_work_space(tmpdir, convert.expected_iso_size(src, fmt), "convert the game")
        rep.note(f"Converting {src.name} ({fmt}) - this can take a while")
        return convert.to_iso(src, tmpdir / "converted.iso", progress=rep.fn, cancel=rep.cancel)
    except (convert.ConvertError, cleanrip.CleanRipError) as e:
        raise GameError(str(e)) from e
    except OSError as e:
        raise GameError(f"Could not read {src.name}: {e}") from e


def _part_targets(files: list[Path], dest_main: Path) -> list[Path]:
    """Where a game's files go when its main file becomes ``dest_main`` (split
    parts keep their .wbfN suffix)."""
    return [dest_main] + [dest_main.with_suffix(p.suffix.lower()) for p in files[1:]]


_same_file = same_file


def _move_files(files: list[Path], dest_main: Path) -> list[Path]:
    """Move a game's files (main + split parts) to a new main name.

    Every target is checked first and a failed move is rolled back, so a game is
    never left split across two places. A target that is the same file as its
    source (a case-only rename on FAT32/NTFS) is fine."""
    targets = _part_targets(files, dest_main)
    for src, dst in zip(files, targets):
        if dst.exists() and not _same_file(src, dst):
            raise GameError(f"{dst.name} already exists in {dst.parent.name}")
    made_dir = not dest_main.parent.exists()
    dest_main.parent.mkdir(parents=True, exist_ok=True)
    done: list[tuple[Path, Path]] = []
    try:
        for src, dst in zip(files, targets):
            if not same_spelling(src, dst):       # not `!=`: on Windows paths compare ignoring case
                rename_path(src, dst)
            done.append((src, dst))
    except BaseException:
        for src, dst in reversed(done):
            with contextlib.suppress(OSError):
                if not same_spelling(src, dst):
                    rename_path(dst, src)
        if made_dir:
            with contextlib.suppress(OSError):
                dest_main.parent.rmdir()
        raise
    return targets


def _replace_files(tmp_paths: list[Path], final_main: Path) -> list[Path]:
    """Move temp files (main + .wbf1...) to final names, replacing old ones.
    If a move fails nothing half-installed is left behind."""
    final_main.parent.mkdir(parents=True, exist_ok=True)
    for old in split_parts(final_main):
        old.unlink(missing_ok=True)
    out: list[Path] = []
    try:
        for i, tp in enumerate(tmp_paths):
            dst = final_main if i == 0 else final_main.with_suffix(f".wbf{i}")
            shutil.move(str(tp), str(dst))
            out.append(dst)
    except BaseException:
        remove_quietly(out)
        raise
    return out


def _rmdir_if_empty(folder: Path, *keep: Path) -> None:
    """Remove a game folder that only has macOS "._" droppings left in it."""
    try:
        if any(_same_file(folder, k) or folder == k for k in keep):
            return
        if not [x for x in folder.iterdir() if not x.name.startswith("._")]:
            shutil.rmtree(folder, ignore_errors=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- install
def install_game(layout: DriveLayout, src: Path, *, title: str | None = None, scrub: bool = True,
                 lookup: TitleLookup | None = None, progress: ProgressFn | None = None,
                 cancel: CancelToken | None = None, overwrite: bool = False,
                 games: list[Game] | None = None) -> Game:
    """Put a Wii or GameCube game on the drive in the ReWii layout.

    ``games`` is an up-to-date :func:`scan_drive` result; pass it when installing
    several games so the drive isn't rescanned for each one. An image that is
    incomplete (truncated, a split part missing) is refused with a GameError."""
    src = Path(src)
    layout.require_root()
    rep = Reporter(progress, cancel)
    staged: list[Path] = []
    with _work_dir() as tdir:
        real = _prepare_source(src, tdir, rep)
        info = identify(real, lookup)
        if info is None or info.needs_conversion:
            raise GameError(f"{src.name} is not a Wii or GameCube game")
        t = title or info.title
        on_drive = games if games is not None else scan_drive(layout)
        try:
            if info.is_wii:
                dest = layout.wii_game_file(t, info.game_id)
                existing = find_on_drive(layout, info.game_id, 0, on_drive)
                if existing and not overwrite:
                    raise GameError(f"{t} is already on the drive")
                need = estimate_install_size(real, scrub)
                if free_space(layout.root) < need + 64 * 1024 * 1024:
                    raise GameError(f"Not enough free space on the drive (needs {need / 1e9:.1f} GB)")
                layout.wbfs.mkdir(parents=True, exist_ok=True)
                with open_disc(real) as d:
                    rep.note(f"Writing {t}")
                    staged = write_wbfs(d, layout.wbfs / f"{TMP_PREFIX}{info.game_id}.wbfs", scrub=scrub,
                                        split=FAT32_SPLIT, progress=progress, cancel=cancel)
                if existing:
                    remove_game(layout, existing)
                files = _replace_files(staged, dest)
            else:
                dest = layout.gc_game_file(t, info.game_id, info.disc_number,
                                           ".ciso" if info.fmt == "CISO" else ".iso")
                existing = find_on_drive(layout, info.game_id, info.disc_number, on_drive)
                if existing and not overwrite:
                    raise GameError(f"{t} (disc {info.disc_number + 1}) is already on the drive")
                with open_disc(real) as d:
                    why = incomplete_reason(d)
                if why:
                    raise GameError(f"Can't copy {t}: {why}")
                if free_space(layout.root) < real.stat().st_size + 16 * 1024 * 1024:
                    raise GameError("Not enough free space on the drive")
                # disc 2 must sit next to disc 1, so reuse disc 1's folder if present
                if info.disc_number:
                    d1 = find_on_drive(layout, info.game_id, 0, on_drive)
                    if d1:
                        dest = d1.path.parent / dest.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(f"{TMP_PREFIX}{dest.name}")
                staged = [tmp]
                rep.set_total(real.stat().st_size, f"Copying {t}")
                copy_file(real, tmp, rep=rep)
                if existing and not _same_file(existing.path, dest):
                    existing.path.unlink(missing_ok=True)
                    _rmdir_if_empty(existing.path.parent, layout.gc_games, dest.parent)
                tmp.replace(dest)
                files = [dest]
        except DiscError as e:
            remove_quietly(staged)
            raise GameError(str(e)) from e
        except OSError as e:
            remove_quietly(staged)
            if not layout.connected():      # unplugged mid-copy: say so, not "no such file"
                raise DriveNotConnected(layout.root) from e
            raise
        except BaseException:
            remove_quietly(staged)           # no-op for files already moved into place
            raise
    g = identify(files[0], lookup, "drive")
    if g is None:
        raise GameError(f"{t} was copied but can't be read back from the drive")
    return g


def find_on_drive(layout: DriveLayout, game_id: str, disc: int = 0,
                  games: list[Game] | None = None) -> Optional[Game]:
    for g in games if games is not None else scan_drive(layout):
        if g.game_id == game_id and g.disc_number == disc:
            return g
    return None


def remove_game(layout: DriveLayout, game: Game) -> None:
    """Delete a game's files from the drive (and its folder if now empty)."""
    root = layout.root.resolve()
    for p in game.files or [game.path]:
        if root not in p.resolve().parents:
            raise GameError("refusing to delete outside the drive")
        p.unlink(missing_ok=True)
    _rmdir_if_empty(game.path.parent, layout.wbfs, layout.gc_games, layout.root)


def _proper_dest(layout: DriveLayout, g: Game, title: str) -> Path:
    """Where a drive game's main file belongs in the ReWii layout."""
    ext = g.path.suffix.lower()
    if g.is_wii:
        return layout.wii_game_file(title, g.game_id, ext)
    return layout.gc_game_file(title, g.game_id, g.disc_number, ".iso" if ext == ".gcm" else ext)


def _is_own_folder(folder: Path, container: Path, game: Game) -> bool:
    """True if ``folder`` is a game folder directly inside ``container`` that holds
    only this game - i.e. renaming the folder renames this game and nothing else."""
    if _same_file(folder, container) or not _same_file(folder.parent, container):
        return False
    mine = {p.name.lower() for p in (game.files or [game.path])}
    try:
        others = [x for x in folder.iterdir() if x.is_file() and x.name.lower() not in mine
                  and x.suffix.lower() in LIBRARY_EXTS and not x.name.startswith(".")]
    except OSError:
        return False
    for x in others:
        o = identify(x)
        if o is not None and o.game_id != game.game_id:
            return False
    return True


def _rename_folder(folder: Path, new: Path) -> None:
    if same_spelling(new, folder):
        return
    if new.exists() and not _same_file(folder, new):       # same file = case-only rename
        raise GameError("a folder with that name already exists")
    rename_path(folder, new)       # also does case-only renames, which FAT32 needs a detour for


def _check_renamable(game: Game) -> None:
    if game.game_id == UNKNOWN_ID or game.needs_conversion or not is_disc_id(game.game_id):
        raise GameError("this game has no readable game ID yet - convert it first, then rename it")


def rename_game(layout: DriveLayout, game: Game, new_title: str) -> Game:
    """Rename a drive game's ``Title [ID]`` folder (the title USB Loader GX shows
    comes from GameTDB, not this).

    Only a folder that belongs to this game alone is renamed. A game lying loose
    in ``wbfs/`` or ``games/``, or sharing a folder with other games, is moved
    into its own properly named folder instead - the container is never renamed."""
    if game.location != "drive":
        raise GameError("only drive games can be renamed here")
    _check_renamable(game)
    folder = game.path.parent
    container = next((c for c in (layout.wbfs, layout.gc_games)
                      if _same_file(folder, c) or _same_file(folder.parent, c)), None)
    if container is not None and _is_own_folder(folder, container, game):
        new = folder.with_name(game_folder_name(new_title, game.game_id))
        _rename_folder(folder, new)
        path = new / game.path.name
    else:
        path = _proper_dest(layout, game, new_title)
        _move_files(game.files or [game.path], path)
        _rmdir_if_empty(folder, layout.wbfs, layout.gc_games, layout.root)
    g = identify(path, None, "drive")
    if g is None:
        raise GameError("the game could not be read after renaming")
    g.title = new_title
    return g


def normalize_drive(layout: DriveLayout, lookup: TitleLookup | None = None,
                    progress: ProgressFn | None = None, cancel: CancelToken | None = None) -> list[str]:
    """Move misplaced games into the proper ``Title [ID]/ID.wbfs`` / ``game.iso`` layout."""
    msgs = []
    games = [g for g in scan_drive(layout, lookup) if g.problem == MISPLACED]
    rep = Reporter(progress, cancel, total=len(games), unit=UNIT_STEPS)
    for g in games:
        rep.check()
        dest = _proper_dest(layout, g, g.title)
        old_folder = g.path.parent
        try:
            # a name that differs only in case is the same file on FAT32: that is a rename
            if dest.exists() and not _same_file(g.path, dest):
                raise GameError(f"{dest} already exists")
            _move_files(g.files or [g.path], dest)
        except (GameError, OSError) as e:
            msgs.append(f"skipped {g.path.name}: {explain_os_error(e, layout.root)}")
            rep.advance(1)
            continue
        msgs.append(f"moved {g.path.relative_to(layout.root)} -> {dest.relative_to(layout.root)}")
        _rmdir_if_empty(old_folder, layout.wbfs, layout.gc_games, layout.root)
        rep.advance(1)
    return msgs


# --------------------------------------------------------------------------- library
def library_path_for(settings: Settings, game: Game) -> Path:
    if game.is_wii:
        return settings.wii_games_dir / f"{game_folder_name(game.title, game.game_id)}.wbfs"
    name = "game.iso" if game.disc_number == 0 else f"disc{game.disc_number + 1}.iso"
    return settings.gc_games_dir / game_folder_name(game.title, game.game_id) / name


def add_to_library(settings: Settings, src: Path, *, move: bool = False, scrub: bool = True,
                   lookup: TitleLookup | None = None, progress: ProgressFn | None = None,
                   cancel: CancelToken | None = None) -> Game:
    """Import a game file into the PC library (Wii -> scrubbed .wbfs, GC -> .iso).

    An incomplete image is refused with a GameError (and never deleted, even
    with ``move``)."""
    src = Path(src)
    rep = Reporter(progress, cancel)
    with _work_dir() as td:
        real = _prepare_source(src, td, rep)
        g = identify(real, lookup)
        if g is None or g.needs_conversion:
            raise GameError(f"{src.name} is not a Wii or GameCube game")
        dest = library_path_for(settings, g)
        if dest.exists():
            raise GameError(f"{g.title} is already in the library")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(TMP_PREFIX + dest.name)
        remove_quietly([tmp])                    # a leftover from an interrupted run
        try:
            with open_disc(real) as d:
                why = incomplete_reason(d)
                if why:
                    raise GameError(f"Can't add {g.title}: {why}")
                if g.is_wii and not (g.fmt == "WBFS" and move and real == src and len(g.files) == 1):
                    write_wbfs(d, tmp, scrub=scrub, split=None, progress=progress, cancel=cancel)
                elif not g.is_wii and g.fmt == "CISO":
                    write_iso(d, tmp, progress=progress, cancel=cancel)
            if tmp.exists():
                tmp.replace(dest)
            elif move and real == src:
                shutil.move(str(src), str(dest))
            else:
                rep.set_total(real.stat().st_size, f"Copying {g.title}")
                copy_file(real, tmp, rep=rep)
                tmp.replace(dest)
        except DiscError as e:
            remove_quietly([tmp])
            raise GameError(str(e)) from e
        except BaseException:
            remove_quietly([tmp])
            raise
        if move and real == src and src.exists():
            for p in split_parts(src):
                p.unlink(missing_ok=True)
    out = identify(dest, lookup, "library")
    if out is None:
        raise GameError(f"{g.title} was copied but can't be read back from the library")
    return out


def copy_to_library(settings: Settings, game: Game, lookup: TitleLookup | None = None,
                    progress: ProgressFn | None = None, cancel: CancelToken | None = None) -> Game:
    """Back a drive game up into the PC library (joins split files)."""
    # scrubbing here only skips sectors the game doesn't use; without it a plain
    # ISO on the drive would be written out at its full 4.7 GB
    return add_to_library(settings, game.path, move=False, scrub=settings.scrub_games, lookup=lookup,
                          progress=progress, cancel=cancel)


def delete_from_library(settings: Settings, game: Game) -> None:
    lib = settings.library.resolve()
    for p in game.files or [game.path]:
        if lib not in p.resolve().parents:
            raise GameError("refusing to delete outside the library")
        p.unlink(missing_ok=True)
    if not game.is_wii:
        try:
            game.path.parent.rmdir()
        except OSError:
            pass


# --------------------------------------------------------------------------- verify
@dataclass
class VerifyResult:
    ok: bool
    checked_clusters: int = 0
    bad_clusters: int = 0
    message: str = ""
    missing_clusters: int = 0       # data the game needs that isn't in the image


def verify_game(path: Path, progress: ProgressFn | None = None,
                cancel: CancelToken | None = None) -> VerifyResult:
    """Check a game's integrity.

    Wii: every cluster the game's file tables use must be in the image (not cut
    off, not blank); every stored cluster of every partition is decrypted and
    the SHA-1 of each 1 KiB block is checked against the cluster's H0 table.
    GameCube discs carry no checksums, so only the header and file table are
    checked, and that the image is long enough to hold every file.
    """
    rep = Reporter(progress, cancel)
    try:
        d = open_disc(path)
    except (DiscError, OSError) as e:           # includes NeedsConversion
        return VerifyResult(False, message=str(e))
    with d:
        try:
            _gid, _t, plat, *_ = parse_header(d.read(0, 0x60))
            if plat == "gc":
                why = incomplete_reason(d)
                return VerifyResult(why is None, message=why or "The image is complete (GameCube discs "
                                                                "have no checksums to verify)")
            if d.missing(0, 0x50000):
                return VerifyResult(False, message=INCOMPLETE)
            # The update partition is optional (scrubbing drops its data but its header
            # shares a WBFS block with the disc header, so it still "exists"): its stored
            # clusters are checksummed, but missing ones are never counted as lost.
            table = wii_usage_table(d, include_update=False)
            # a partition whose header the image doesn't store was removed by a scrub
            parts = [p for p in wii_partitions(d)
                     if p.title_key and d.sector_present(p.offset // WII_SECTOR)]
        except DiscError as e:
            return VerifyResult(False, message=str(e))
        if not parts:
            return VerifyResult(False, message="no readable partitions")
        todo = []
        for p in parts:
            first = p.data_offset // WII_SECTOR
            n = max(0, min((p.data_size + WII_SECTOR - 1) // WII_SECTOR, WII_DL_SECTORS - first))
            for i in range(n):
                needed = table[first + i]
                if needed or d.sector_present(first + i):
                    todo.append((p, i, needed))
        rep.set_total(len(todo), "Verifying", unit=UNIT_STEPS)
        bad = checked = lost = 0
        for k, (p, i, needed) in enumerate(todo, 1):
            if k % 64 == 0 or k == len(todo):
                rep.update(k)                   # also checks for cancel
            off = p.data_offset + i * WII_SECTOR
            if needed and (d.missing(off, WII_SECTOR) or not d.sector_present(off // WII_SECTOR)):
                lost += 1
                continue
            raw = d.read(off, WII_SECTOR)
            if raw.count(0) == len(raw):
                lost += needed          # blank: fine if scrubbed away, not if the game uses it
                continue
            hashes = aes_cbc_decrypt(p.title_key, bytes(16), raw[:0x400])
            data = aes_cbc_decrypt(p.title_key, raw[0x3D0:0x3E0], raw[0x400:])
            checked += 1
            for b in range(31):
                if hashlib.sha1(data[b * 0x400:(b + 1) * 0x400]).digest() != hashes[b * 20:(b + 1) * 20]:
                    bad += 1
                    break
        problems = []
        if lost:
            problems.append(f"{lost} clusters the game needs are missing - {INCOMPLETE}")
        if bad:
            problems.append(f"{bad} damaged clusters")
        return VerifyResult(not problems, checked, bad, "; ".join(problems) or "All data checks out", lost)


# --------------------------------------------------------------------------- legacy migration
CUT_OFF = ("the old drive (or its image file) is cut off before the end of this game, so the "
           "game's data isn't all there. Copy the whole drive again and retry.")


class MigrateResult(list[str]):
    """The lines :func:`migrate_wbfs_partition` reports, plus how many games were
    skipped and how many were copied but turned out to be damaged."""

    def __init__(self) -> None:
        super().__init__()
        self.skipped = 0
        self.damaged = 0

    @property
    def problems(self) -> int:
        return self.skipped + self.damaged


def _cut_off(part: WbfsPartition, slot: int) -> bool:
    try:
        r = part.open_game(slot)
        return r.missing(0, r.size)
    except DiscError:
        return False


def migrate_wbfs_partition(source: Path, layout: DriveLayout | None = None,
                           settings: Settings | None = None, slots: list[int] | None = None,
                           offset: int = 0, lookup: TitleLookup | None = None,
                           progress: ProgressFn | None = None, cancel: CancelToken | None = None,
                           *, games: list[Game] | None = None) -> MigrateResult:
    """Copy games out of an old WBFS-formatted drive (or disk image) to the new
    layout on ``layout`` and/or into the library.

    Games already there (matched by game ID) are left alone. A game that can't
    be read or doesn't fit is skipped with a line in the result; the others are
    still copied. ``games`` is an optional current :func:`scan_drive` result."""
    if layout is not None:
        layout.require_root()
    part = WbfsPartition(source, offset)
    out = MigrateResult()
    try:
        todo = [g for g in part.games() if slots is None or g.slot in slots]
        out += [f"slot {slot}: skipped - {why}" for slot, why in part.skipped
                if slots is None or slot in slots]
        out.skipped += len(out)
        on_drive = {g.game_id for g in (games if games is not None else scan_drive(layout))
                    if g.is_wii} if layout is not None else set()
        in_lib = {g.game_id for g in scan_library(settings) if g.is_wii} if settings is not None else set()
        rep = Reporter(progress, cancel)
        for i, pg in enumerate(todo, 1):
            rep.check()
            title = _title(lookup, pg.game_id, pg.title)
            rep.note(f"[{i}/{len(todo)}] {title}")
            need = pg.size + (2 << 20) + 64 * 1024 * 1024
            jobs: list[tuple[str, Path, Path, Path, int | None]] = []
            if layout is not None and pg.game_id not in on_drive:
                jobs.append(("drive", layout.root, layout.wii_game_file(title, pg.game_id),
                             layout.wbfs / f"{TMP_PREFIX}{pg.game_id}.wbfs", FAT32_SPLIT))
            if settings is not None and pg.game_id not in in_lib:
                dest = settings.wii_games_dir / f"{game_folder_name(title, pg.game_id)}.wbfs"
                jobs.append(("library", settings.wii_games_dir, dest, dest.with_name(TMP_PREFIX + dest.name), None))
            if jobs and _cut_off(part, pg.slot):
                # readers give zeros past the end, which would otherwise surface as a
                # baffling "no Wii/GameCube magic" (or be copied as a broken game)
                out.append(f"{title}: skipped - {CUT_OFF}")
                out.skipped += 1
                continue
            verdict: VerifyResult | None = None       # the copies are identical: check one
            for where, space_root, dest, staging, split in jobs:
                tmp: list[Path] = []
                try:
                    if dest.exists():
                        continue
                    if free_space(space_root) < need:
                        raise GameError(f"not enough free space ({need / 1e9:.1f} GB needed)")
                    if layout is not None and where == "drive":
                        layout.require_root()
                    staging.parent.mkdir(parents=True, exist_ok=True)
                    tmp = write_wbfs(part.open_game(pg.slot), staging, scrub=False, split=split,
                                     progress=progress, cancel=cancel)
                    files = _replace_files(tmp, dest)
                    if verdict is None:
                        rep.note(f"[{i}/{len(todo)}] Checking {title}")
                        verdict = verify_game(files[0], progress, cancel)
                    if verdict.ok:
                        out.append(f"{title} -> {where}")
                    else:
                        out.append(f"{title} -> {where}, but the game is DAMAGED on the old drive "
                                   f"({verdict.message}). The copy was kept; it may crash or not "
                                   "start - rip the disc again if you can.")
                        out.damaged += 1
                except (DiscError, GameError, OSError) as e:      # Cancelled is not caught here
                    remove_quietly(tmp)
                    why = explain_os_error(e, layout.root if layout is not None and where == "drive"
                                           else None)
                    out.append(f"{title}: skipped ({where}) - {why}")
                    out.skipped += 1
                except BaseException:
                    remove_quietly(tmp)
                    raise
    finally:
        part.close()
    return out


def rename_library_game(settings: Settings, game: Game, new_title: str) -> Game:
    """Rename a library game's file (Wii, with any split parts) or folder (GameCube).

    A GameCube game lying loose in the library's GameCube folder, or sharing a
    folder with other games, is moved into its own ``Title [ID]`` folder - the
    library folder itself is never renamed."""
    if game.location != "library":
        raise GameError("not a library game")
    _check_renamable(game)
    files = game.files or [game.path]
    if game.is_wii:
        path = game.path.with_name(f"{game_folder_name(new_title, game.game_id)}{game.path.suffix}")
        if not same_spelling(path, game.path):
            _move_files(files, path)
    else:
        folder = game.path.parent
        container = settings.gc_games_dir
        if _is_own_folder(folder, container, game):
            new = folder.with_name(game_folder_name(new_title, game.game_id))
            _rename_folder(folder, new)
            path = new / game.path.name
        else:
            renamed = Game(**{**game.__dict__, "title": new_title})
            path = library_path_for(settings, renamed).with_suffix(game.path.suffix.lower())
            _move_files(files, path)
            _rmdir_if_empty(folder, container, settings.library)
    g = identify(path, None, "library")
    if g is None:
        raise GameError("the game could not be read after renaming")
    g.title = new_title
    return g
