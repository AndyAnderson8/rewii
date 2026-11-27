"""Command-line interface for scripting (``rewii <command> ...``). Running ``rewii``
with no arguments opens the full-screen app instead."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .core.config import Settings
from .core.layout import DriveLayout
from .core.progress import ProgressEvent


class _Bar:
    def __init__(self) -> None:
        self.last = ""

    def __call__(self, ev: ProgressEvent) -> None:
        if not sys.stderr.isatty():
            return
        pct = f"{ev.fraction * 100:5.1f}%" if ev.total else "     "
        line = f"\r{pct} {ev.message[:70]:<70}"
        if line != self.last:
            sys.stderr.write(line)
            sys.stderr.flush()
            self.last = line

    def done(self) -> None:
        if self.last and sys.stderr.isatty():
            sys.stderr.write("\r" + " " * 80 + "\r")


def _drive(args) -> DriveLayout:
    d = args.drive or Settings.load().last_drive
    if not d:
        sys.exit("error: give --drive PATH (the Wii drive's mount point / drive letter)")
    return DriveLayout(Path(d))


def _titles(settings: Settings):
    try:
        from .core.gametdb import TitleDB
        db = TitleDB.load(settings)
        return lambda gid: db.title(gid, None) if db else None
    except Exception:
        return None


def cmd_info(a) -> int:
    from .core.disc import NeedsConversion, disc_info
    for p in a.files:
        try:
            i = disc_info(p)
            print(f"{p}: {i.title} [{i.game_id}] {i.platform} disc {i.disc_number + 1} {i.region} {i.fmt}")
        except NeedsConversion as e:
            print(f"{p}: {e}")
        except Exception as e:
            print(f"{p}: not a game ({e})")
    return 0


def cmd_games_list(a) -> int:
    from .core import games as G
    s = Settings.load()
    look = _titles(s)
    rows = G.scan_drive(_drive(a), look) if a.drive or not a.library else []
    if a.library:
        rows = G.scan_library(s, look)
    for g in rows:
        print(f"{g.game_id}  {'Wii' if g.is_wii else 'GC '}  {g.size / 1e9:6.2f} GB  {g.title}"
              f"{'  [' + g.problem + ']' if g.problem else ''}")
    return 0


def cmd_games_add(a) -> int:
    from .core import games as G
    s = Settings.load()
    look = _titles(s)
    bar = _Bar()
    rc = 0
    for p in a.files:
        try:
            if a.library_only or not a.drive:
                g = G.add_to_library(s, Path(p), scrub=s.scrub_games, lookup=look, progress=bar)
            else:
                g = G.install_game(_drive(a), Path(p), scrub=s.scrub_games, lookup=look, progress=bar)
            bar.done()
            print(f"added {g.title} [{g.game_id}] -> {g.path}")
        except G.GameError as e:
            bar.done()
            print(f"{p}: {e}", file=sys.stderr)
            rc = 1
    return rc


def cmd_games_remove(a) -> int:
    from .core import games as G
    lay = _drive(a)
    games = G.scan_drive(lay)
    for gid in a.ids:
        hits = [g for g in games if g.game_id == gid.upper()]
        if not hits:
            print(f"{gid}: not on the drive", file=sys.stderr)
            continue
        for g in hits:
            if a.yes or input(f"Delete {g.title} from the drive? [y/N] ").lower() == "y":
                G.remove_game(lay, g)
                print(f"removed {g.title}")
    return 0


def cmd_verify(a) -> int:
    from .core.games import verify_game
    bar = _Bar()
    rc = 0
    for p in a.files:
        r = verify_game(Path(p), bar)
        bar.done()
        print(f"{'OK ' if r.ok else 'BAD'} {p}: {r.message}")
        rc |= not r.ok
    return rc


def cmd_channel(a) -> int:
    from .core.forwarder import make_channel
    s = Settings.load()
    out = Path(a.out) if a.out else s.channels_dir
    style = a.style or s.forwarder_style
    for p in a.files:
        cov = None
        if style == "cover":
            try:
                from .core.disc import disc_info
                from .core import covers
                cp = covers.cached_cover_path(disc_info(p).game_id, "cover", fetch=True)
                cov = cp.read_bytes() if cp else None
            except Exception:
                cov = None
        ci = make_channel(Path(p), out, style=style, cover_png=cov, fat=not a.wbfs_partition)
        print(f"{ci.path}  ({ci.channel_id}, {style})")
    return 0


def cmd_normalize(a) -> int:
    from .core.games import normalize_drive
    for m in normalize_drive(_drive(a)):
        print(m)
    return 0


def cmd_migrate(a) -> int:
    from .core.games import migrate_wbfs_partition
    from .core.wbfs import WbfsPartition, find_wbfs_partition
    off = find_wbfs_partition(a.source)
    if off is None:
        sys.exit("no WBFS partition found")
    if a.list:
        p = WbfsPartition(a.source, off)
        for g in p.games():
            print(f"{g.slot:3}  {g.game_id}  {g.size / 1e9:6.2f} GB  {g.title}")
        return 0
    s = Settings.load()
    bar = _Bar()
    res = migrate_wbfs_partition(Path(a.source), _drive(a) if a.drive else None, s, None, off,
                                 _titles(s), bar)
    bar.done()
    print("\n".join(res))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rewii", description="ReWii - all-in-one Wii toolkit. "
                                 "Run without arguments for the full app.")
    ap.add_argument("--version", action="version", version=f"ReWii {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info", help="show what game a file is")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_info)

    g = sub.add_parser("games", help="manage games").add_subparsers(dest="sub", required=True)
    p = g.add_parser("list", help="list games on the drive (or --library)")
    p.add_argument("--drive")
    p.add_argument("--library", action="store_true")
    p.set_defaults(fn=cmd_games_list)
    p = g.add_parser("add", help="add game files to the library, or to --drive")
    p.add_argument("files", nargs="+")
    p.add_argument("--drive")
    p.add_argument("--library-only", action="store_true")
    p.set_defaults(fn=cmd_games_add)
    p = g.add_parser("remove", help="remove games (by ID) from the drive")
    p.add_argument("ids", nargs="+")
    p.add_argument("--drive")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(fn=cmd_games_remove)
    p = g.add_parser("fix-layout", help="move misplaced games into the right folders")
    p.add_argument("--drive")
    p.set_defaults(fn=cmd_normalize)
    p = g.add_parser("migrate", help="copy games out of an old WBFS-formatted drive/image")
    p.add_argument("source")
    p.add_argument("--drive")
    p.add_argument("--list", action="store_true")
    p.set_defaults(fn=cmd_migrate)

    p = sub.add_parser("verify", help="check game files for damage")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("channel", help="make a Wii Menu channel (WAD) for a game")
    p.add_argument("files", nargs="+")
    p.add_argument("--style", choices=["cover", "black", "classic"])
    p.add_argument("--out")
    p.add_argument("--wbfs-partition", action="store_true",
                   help="for old WBFS-formatted drives instead of FAT32")
    p.set_defaults(fn=cmd_channel)
    return ap


def main(argv: list[str]) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    try:
        return int(a.fn(a) or 0)
    except KeyboardInterrupt:
        return 130
