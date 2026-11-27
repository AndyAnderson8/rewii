"""Wii and GameCube game management: the PC library and the Wii's drive.

Library (on the PC):   <library>/Games/Wii/Title [ID].wbfs, <library>/Games/GameCube/Title [ID]/game.iso
Drive (USB):           wbfs/Title [ID]/ID.wbfs (+.wbf1...), games/Title [ID]/game.iso (+disc2.iso)
"""
from __future__ import annotations

import hashlib
import re
import shutil
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .config import Settings
from .disc import (FAT32_SPLIT, WII_SECTOR, WII_SECTOR_DATA, DiscError, DiscInfo, NeedsConversion,
                   aes_cbc_decrypt, disc_info, open_disc, parse_header, region_of, sniff_format,
                   split_parts, used_sectors, wii_partitions)
from .layout import DriveLayout, game_folder_name, id_from_folder, safe_name
from .progress import CancelToken, ProgressFn, Reporter
from .wbfs import WbfsPartition, copy_file, write_iso, write_wbfs

WII_EXTS = {".wbfs", ".iso", ".ciso", ".wdf", ".rvz", ".wia", ".gcz", ".nkit", ".gcm"}
DRIVE_WII_EXTS = {".wbfs", ".iso", ".ciso"}
DRIVE_GC_EXTS = {".iso", ".gcm", ".ciso"}
CONVERTIBLE = {"RVZ", "WIA", "GCZ", "NKIT", "WDF"}

TitleLookup = Callable[[str], Optional[str]]


class GameError(Exception):
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
    problem: str = ""          # e.g. "needs conversion", "misplaced"

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
    size = sum(p.stat().st_size for p in files)
    if fmt in CONVERTIBLE:
        gid = id_from_folder(path.stem) or id_from_folder(path.parent.name) or "??????"
        plat = "gc" if gid.startswith(("G", "D")) else "wii"
        t = _title(lookup, gid, path.stem)
        return Game(gid, t, path.stem, plat, 0, region_of(gid), fmt, path, files, size, location,
                    "needs conversion")
    if fmt not in ("ISO", "WBFS", "CISO"):
        return None
    try:
        with open_disc(path) as d:
            gid, dt, plat, num, _ver = parse_header(d.read(0, 0x60))
    except (DiscError, OSError, struct.error):
        return None
    return Game(gid, _title(lookup, gid, dt), dt, plat, num, region_of(gid), fmt, path, files, size, location)


def _iter_images(root: Path, exts: set[str]) -> Iterable[Path]:
    if not root.exists():
        return
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in exts and not p.name.startswith("._"):
            yield p


def scan_library(settings: Settings, lookup: TitleLookup | None = None) -> list[Game]:
    out: list[Game] = []
    seen: set[Path] = set()
    for root in (settings.wii_games_dir, settings.gc_games_dir):
        for p in _iter_images(root, WII_EXTS):
            if p in seen:
                continue
            g = identify(p, lookup, "library")
            if g:
                seen.update(g.files)
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
                or p.stem != g.game_id:
            g.problem = g.problem or "misplaced"
        out.append(g)
    for p in _iter_images(layout.gc_games, DRIVE_GC_EXTS | CONVERTIBLE_EXTS):
        g = identify(p, lookup, "drive")
        if not g:
            continue
        _folder_title(g, p)
        good_name = p.stem in ("game", "disc2")
        if p.parent.parent != layout.gc_games or not good_name:
            g.problem = g.problem or "misplaced"
        out.append(g)
    return sorted(out, key=lambda g: (g.platform != "wii", g.title.lower(), g.disc_number))


CONVERTIBLE_EXTS = {".rvz", ".wia", ".gcz", ".nkit", ".wdf"}


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
    """Bytes a game will take on the drive."""
    g_fmt = sniff_format(path)
    if g_fmt in CONVERTIBLE:
        return sum(p.stat().st_size for p in split_parts(path)) * 3  # rough upper bound
    with open_disc(path) as d:
        _gid, _t, plat, *_ = parse_header(d.read(0, 0x60))
        if plat == "gc":
            return d.size
        used = used_sectors(d, scrub=scrub)
        blocks = {i * WII_SECTOR // (2 << 20) for i, u in enumerate(used) if u}
        return (len(blocks) + 1) * (2 << 20)


_PART_RE = re.compile(r"\.part\d+\.iso$", re.I)


def _prepare_source(src: Path, tmpdir: Path, rep: Reporter) -> Path:
    """Join CleanRip part files / convert RVZ/NKit/etc. to ISO in a temp folder if needed."""
    if _PART_RE.search(src.name):
        from . import cleanrip
        dump = next((d for d in cleanrip.find_dumps(src.parent) if src in d.parts), None)
        if dump is not None and dump.is_split:
            if not dump.complete:
                raise GameError(f"{dump.name}: dump parts {dump.missing_parts} are missing")
            rep.note(f"Joining CleanRip dump {dump.name}")
            out = tmpdir / f"{dump.name}.iso"
            res = cleanrip.join_dump(dump, out, rep.fn, rep.cancel)
            if res.dumpinfo_match is False:
                raise GameError(f"{dump.name}: the dump is damaged ({', '.join(res.mismatches)}) - rip it again")
            return out
    fmt = sniff_format(src)
    if fmt not in CONVERTIBLE:
        return src
    try:
        from . import convert
    except ImportError as e:  # pragma: no cover
        raise GameError("Conversion support is missing") from e
    rep.note(f"Converting {src.name} ({fmt}) - this can take a while")
    out = tmpdir / (src.stem + ".iso")
    return convert.to_iso(src, out, progress=rep.fn, cancel=rep.cancel)


def _replace_files(tmp_paths: list[Path], final_main: Path) -> list[Path]:
    """Move temp files (main + .wbf1...) to final names, replacing old ones."""
    final_main.parent.mkdir(parents=True, exist_ok=True)
    for old in split_parts(final_main):
        old.unlink(missing_ok=True)
    out = []
    for i, tp in enumerate(tmp_paths):
        dst = final_main if i == 0 else final_main.with_suffix(f".wbf{i}")
        shutil.move(str(tp), str(dst))
        out.append(dst)
    return out


# --------------------------------------------------------------------------- install
def install_game(layout: DriveLayout, src: Path, *, title: str | None = None, scrub: bool = True,
                 lookup: TitleLookup | None = None, progress: ProgressFn | None = None,
                 cancel: CancelToken | None = None, overwrite: bool = False) -> Game:
    """Put a Wii or GameCube game on the drive in the ReWii layout."""
    src = Path(src)
    rep = Reporter(progress, cancel)
    with tempfile.TemporaryDirectory(prefix="rewii-") as td:
        tdir = Path(td)
        real = _prepare_source(src, tdir, rep)
        info = identify(real, lookup)
        if info is None:
            raise GameError(f"{src.name} is not a Wii or GameCube game")
        t = title or info.title
        if info.is_wii:
            dest = layout.wii_game_file(t, info.game_id)
            existing = find_on_drive(layout, info.game_id, 0)
            if existing and not overwrite:
                raise GameError(f"{t} is already on the drive")
            need = estimate_install_size(real, scrub)
            if free_space(layout.root) < need + 64 * 1024 * 1024:
                raise GameError(f"Not enough free space on the drive (needs {need / 1e9:.1f} GB)")
            if info.fmt == "WBFS" and not scrub:
                staging = layout.wbfs / f".rewii-tmp-{info.game_id}.wbfs"
            else:
                staging = layout.wbfs / f".rewii-tmp-{info.game_id}.wbfs"
            layout.wbfs.mkdir(parents=True, exist_ok=True)
            with open_disc(real) as d:
                rep.note(f"Writing {t}")
                tmp = write_wbfs(d, staging, scrub=scrub, split=FAT32_SPLIT,
                                 progress=progress, cancel=cancel)
            if existing:
                remove_game(layout, existing)
            files = _replace_files(tmp, dest)
        else:
            dest = layout.gc_game_file(t, info.game_id, info.disc_number,
                                       ".ciso" if info.fmt == "CISO" else ".iso")
            existing = find_on_drive(layout, info.game_id, info.disc_number)
            if existing and not overwrite:
                raise GameError(f"{t} (disc {info.disc_number + 1}) is already on the drive")
            if free_space(layout.root) < real.stat().st_size + 16 * 1024 * 1024:
                raise GameError("Not enough free space on the drive")
            # disc 2 must sit next to disc 1, so reuse disc 1's folder if present
            if info.disc_number:
                d1 = find_on_drive(layout, info.game_id, 0)
                if d1:
                    dest = d1.path.parent / dest.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f".rewii-tmp-{dest.name}")
            rep.set_total(real.stat().st_size, f"Copying {t}")
            copy_file(real, tmp, rep=rep)
            if existing:
                existing.path.unlink(missing_ok=True)
            tmp.replace(dest)
            files = [dest]
    g = identify(files[0], lookup, "drive")
    assert g is not None
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
    folder = game.path.parent
    if folder.resolve() not in (layout.wbfs.resolve(), layout.gc_games.resolve()):
        try:
            leftovers = [x for x in folder.iterdir() if not x.name.startswith("._")]
            if not leftovers:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass


def rename_game(layout: DriveLayout, game: Game, new_title: str) -> Game:
    """Rename a game's folder (the title USB Loader GX shows comes from GameTDB, not this)."""
    if game.location != "drive":
        raise GameError("only drive games can be renamed here")
    folder = game.path.parent
    new = folder.with_name(game_folder_name(new_title, game.game_id))
    if new != folder:
        if new.exists():
            raise GameError("a folder with that name already exists")
        folder.rename(new)
    g = identify(new / game.path.name, None, "drive")
    assert g
    g.title = new_title
    return g


def normalize_drive(layout: DriveLayout, lookup: TitleLookup | None = None,
                    progress: ProgressFn | None = None, cancel: CancelToken | None = None) -> list[str]:
    """Move misplaced games into the proper ``Title [ID]/ID.wbfs`` / ``game.iso`` layout."""
    msgs = []
    games = [g for g in scan_drive(layout, lookup) if g.problem == "misplaced"]
    rep = Reporter(progress, cancel, total=len(games))
    for g in games:
        rep.check()
        if g.is_wii:
            dest = layout.wii_game_file(g.title, g.game_id, g.path.suffix.lower())
        else:
            dest = layout.gc_game_file(g.title, g.game_id, g.disc_number, g.path.suffix.lower()
                                       if g.path.suffix.lower() != ".gcm" else ".iso")
        if dest.exists():
            msgs.append(f"skipped {g.path.name}: {dest} already exists")
            rep.advance(1)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        old_folder = g.path.parent
        for i, p in enumerate(g.files):
            target = dest if i == 0 else dest.with_suffix(f".wbf{i}")
            shutil.move(str(p), str(target))
        msgs.append(f"moved {g.path.relative_to(layout.root)} -> {dest.relative_to(layout.root)}")
        try:
            if old_folder not in (layout.wbfs, layout.gc_games) and not any(old_folder.iterdir()):
                old_folder.rmdir()
        except OSError:
            pass
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
    """Import a game file into the PC library (Wii -> scrubbed .wbfs, GC -> .iso)."""
    src = Path(src)
    rep = Reporter(progress, cancel)
    with tempfile.TemporaryDirectory(prefix="rewii-") as td:
        real = _prepare_source(src, Path(td), rep)
        g = identify(real, lookup)
        if g is None:
            raise GameError(f"{src.name} is not a Wii or GameCube game")
        dest = library_path_for(settings, g)
        if dest.exists():
            raise GameError(f"{g.title} is already in the library")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(".rewii-tmp-" + dest.name)
        if g.is_wii:
            if g.fmt == "WBFS" and move and real == src and len(g.files) == 1:
                shutil.move(str(src), str(dest))
            else:
                with open_disc(real) as d:
                    paths = write_wbfs(d, tmp, scrub=scrub, split=None, progress=progress, cancel=cancel)
                paths[0].replace(dest)
        else:
            if g.fmt == "CISO":
                with open_disc(real) as d:
                    paths = write_iso(d, tmp, progress=progress, cancel=cancel)
                paths[0].replace(dest)
            elif move and real == src:
                shutil.move(str(src), str(dest))
            else:
                rep.set_total(real.stat().st_size, f"Copying {g.title}")
                copy_file(real, tmp, rep=rep)
                tmp.replace(dest)
        if move and real == src and src.exists():
            for p in split_parts(src):
                p.unlink(missing_ok=True)
    out = identify(dest, lookup, "library")
    assert out
    return out


def copy_to_library(settings: Settings, game: Game, lookup: TitleLookup | None = None,
                    progress: ProgressFn | None = None, cancel: CancelToken | None = None) -> Game:
    """Back a drive game up into the PC library (joins split files)."""
    return add_to_library(settings, game.path, move=False, scrub=False, lookup=lookup,
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


def export_iso(game: Game, dest: Path, progress: ProgressFn | None = None,
               cancel: CancelToken | None = None) -> Path:
    """Write a full ISO (e.g. for Dolphin)."""
    with open_disc(game.path) as d:
        return write_iso(d, Path(dest), progress=progress, cancel=cancel)[0]


# --------------------------------------------------------------------------- verify
@dataclass
class VerifyResult:
    ok: bool
    checked_clusters: int = 0
    bad_clusters: int = 0
    message: str = ""


def verify_game(path: Path, progress: ProgressFn | None = None,
                cancel: CancelToken | None = None) -> VerifyResult:
    """Check a game's integrity.

    Wii: decrypts every present cluster of every partition and checks the SHA-1
    hashes of each 1 KiB block against the cluster's H0 table.
    GameCube: checks the header and that the image size is plausible.
    """
    rep = Reporter(progress, cancel)
    try:
        d = open_disc(path)
    except NeedsConversion as e:
        return VerifyResult(False, message=str(e))
    except (DiscError, OSError) as e:
        return VerifyResult(False, message=str(e))
    with d:
        try:
            _gid, _t, plat, *_ = parse_header(d.read(0, 0x60))
        except DiscError as e:
            return VerifyResult(False, message=str(e))
        if plat == "gc":
            ok = d.size >= 0x440
            return VerifyResult(ok, message="GameCube header OK" if ok else "image too small")
        parts = [p for p in wii_partitions(d) if p.title_key]
        if not parts:
            return VerifyResult(False, message="no readable partitions")
        todo = []
        for p in parts:
            n = p.data_size // WII_SECTOR
            first = p.data_offset // WII_SECTOR
            todo += [(p, i) for i in range(n) if d.sector_present(first + i)]
        rep.set_total(len(todo), "Verifying")
        bad = checked = 0
        for p, i in todo:
            raw = d.read(p.data_offset + i * WII_SECTOR, WII_SECTOR)
            rep.advance(1)
            if not any(raw):
                continue      # scrubbed
            hashes = aes_cbc_decrypt(p.title_key, bytes(16), raw[:0x400])
            data = aes_cbc_decrypt(p.title_key, raw[0x3D0:0x3E0], raw[0x400:])
            checked += 1
            for b in range(31):
                if hashlib.sha1(data[b * 0x400:(b + 1) * 0x400]).digest() != hashes[b * 20:(b + 1) * 20]:
                    bad += 1
                    break
        ok = bad == 0
        return VerifyResult(ok, checked, bad,
                            "All data checks out" if ok else f"{bad} damaged clusters")


# --------------------------------------------------------------------------- legacy migration
def migrate_wbfs_partition(source: Path, layout: DriveLayout | None = None,
                           settings: Settings | None = None, slots: list[int] | None = None,
                           offset: int = 0, lookup: TitleLookup | None = None,
                           progress: ProgressFn | None = None, cancel: CancelToken | None = None) -> list[str]:
    """Copy games out of an old WBFS-formatted drive (or disk image) to the new
    layout on ``layout`` and/or into the library."""
    part = WbfsPartition(source, offset)
    out = []
    try:
        games = [g for g in part.games() if slots is None or g.slot in slots]
        rep = Reporter(progress, cancel)
        for i, pg in enumerate(games, 1):
            title = _title(lookup, pg.game_id, pg.title)
            rep.note(f"[{i}/{len(games)}] {title}")
            reader = part.open_game(pg.slot)
            if layout is not None:
                dest = layout.wii_game_file(title, pg.game_id)
                if not dest.exists():
                    layout.wbfs.mkdir(parents=True, exist_ok=True)
                    tmp = write_wbfs(reader, layout.wbfs / f".rewii-tmp-{pg.game_id}.wbfs", scrub=False,
                                     split=FAT32_SPLIT, progress=progress, cancel=cancel)
                    _replace_files(tmp, dest)
                    out.append(f"{title} -> drive")
            if settings is not None:
                dest = settings.wii_games_dir / f"{game_folder_name(title, pg.game_id)}.wbfs"
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = write_wbfs(reader, dest.with_name(".rewii-tmp-" + dest.name), scrub=False,
                                     split=None, progress=progress, cancel=cancel)
                    tmp[0].replace(dest)
                    out.append(f"{title} -> library")
    finally:
        part.close()
    return out
