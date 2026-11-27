"""Command-line interface for scripting (``rewii <command> ...``). Running ``rewii``
with no arguments opens the full-screen app instead.

Exit codes: 0 = done, 1 = the operation failed (or found problems), 2 = the
command was used wrongly (bad arguments, missing drive, needs ``-y``), 130 = Ctrl+C.
Mistakes a user can make (wrong path, not a game, drive missing...) print one
``error:`` line - never a traceback.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Optional
from collections.abc import Callable

from . import __version__
from .core.config import Settings
from .core.fileops import copy_atomic
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


class CliError(Exception):
    """A problem to report as ``error: ...`` with an exit code (2 = wrong usage)."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def is_user_error(e: BaseException) -> bool:
    """Errors that describe the user's files/drive rather than a bug: anything the OS
    refuses (missing file, no permission, disk full) and every error type the core defines."""
    return isinstance(e, OSError) or type(e).__module__.startswith("rewii.core.")


def _say(e: BaseException, drive: Any = None) -> str:
    """One plain line for an error (``drive``: the DriveLayout or root it happened on)."""
    from .core.fileops import DriveNotConnected, explain_os_error
    root = getattr(drive, "root", drive)
    if isinstance(e, DriveNotConnected):
        return str(e)
    if isinstance(e, FileNotFoundError) and e.filename and (root is None or Path(root).is_dir()):
        return f"{e.filename}: no such file or folder"
    if isinstance(e, PermissionError) and e.filename:
        return f"{e.filename}: permission denied (a drive read directly needs admin rights)"
    return explain_os_error(e, root)


def _fail(what: object, e: BaseException, drive: Any = None) -> None:
    print(f"{what}: {_say(e, drive)}", file=sys.stderr)


def _drive_at(path: str) -> DriveLayout:
    p = Path(path).expanduser()
    if not p.is_dir():
        raise CliError(f"the drive {p} isn't there (is it plugged in and mounted?)")
    return DriveLayout(p)


def _drive(args: Any) -> DriveLayout:
    d = args.drive or Settings.load().last_drive
    if not d:
        raise CliError("give --drive PATH (the Wii drive's mount point / drive letter)")
    return _drive_at(d)


def _titles(settings: Settings) -> Optional[Callable[[str], Optional[str]]]:
    try:
        from .core.gametdb import TitleDB
        db = TitleDB.load(settings)
    except Exception:  # noqa: BLE001 - titles then come from the discs
        return None

    def look(gid: str) -> Optional[str]:
        g = db.get(gid)
        return g.title if g else None
    return look


def cmd_info(a: Any) -> int:
    from .core.disc import NeedsConversion, disc_info
    rc = 0
    for p in a.files:
        try:
            i = disc_info(p)
            print(f"{p}: {i.title} [{i.game_id}] {i.platform} disc {i.disc_number + 1} {i.region} {i.fmt}")
        except NeedsConversion as e:
            print(f"{p}: {e}")
        except Exception as e:  # noqa: BLE001 - whatever it is, it isn't a readable game
            print(f"{p}: not a game ({_say(e)})", file=sys.stderr)
            rc = 1
    return rc


def cmd_games_list(a: Any) -> int:
    from .core import games as G
    s = Settings.load()
    if a.library:
        rows = G.scan_library(s, _titles(s))
    else:
        lay = _drive(a)
        rows = G.scan_drive(lay, _titles(s))
    for g in rows:
        print(f"{g.game_id}  {'Wii' if g.is_wii else 'GC '}  {g.size / 1e9:6.2f} GB  {g.title}"
              f"{'  [' + g.problem + ']' if g.problem else ''}")
    return 0


def cmd_games_add(a: Any) -> int:
    from .core import games as G
    s = Settings.load()
    lay = _drive_at(a.drive) if a.drive and not a.library_only else None
    look = _titles(s)
    bar = _Bar()
    rc = 0
    for p in a.files:
        try:
            if lay is None:
                g = G.add_to_library(s, Path(p), scrub=s.scrub_games, lookup=look, progress=bar)
            else:
                g = G.install_game(lay, Path(p), scrub=s.scrub_games, lookup=look, progress=bar)
            bar.done()
            print(f"added {g.title} [{g.game_id}] -> {g.path}")
        except Exception as e:  # noqa: BLE001
            bar.done()
            if not is_user_error(e):
                raise
            _fail(p, e, lay)
            rc = 1
    return rc


def cmd_games_remove(a: Any) -> int:
    from .core import games as G
    lay = _drive(a)
    if not a.yes and not sys.stdin.isatty():
        raise CliError("removing games asks for confirmation; pass -y to remove without asking")
    games = G.scan_drive(lay)
    rc = 0
    for gid in a.ids:
        hits = [g for g in games if g.game_id == gid.upper()]
        if not hits:
            print(f"{gid}: not on the drive", file=sys.stderr)
            rc = 1
            continue
        for g in hits:
            if not a.yes:
                try:
                    answer = input(f"Delete {g.title} from the drive? [y/N] ")
                except EOFError:
                    raise CliError("no answer; pass -y to remove without asking") from None
                if answer.strip().lower() not in ("y", "yes"):
                    print(f"kept {g.title}")
                    continue
            try:
                G.remove_game(lay, g)
                print(f"removed {g.title}")
            except Exception as e:  # noqa: BLE001
                if not is_user_error(e):
                    raise
                _fail(g.title, e, lay)
                rc = 1
    return rc


def cmd_verify(a: Any) -> int:
    from .core.games import verify_game
    bar = _Bar()
    rc = 0
    for p in a.files:
        r = verify_game(Path(p), bar)
        bar.done()
        print(f"{'OK ' if r.ok else 'BAD'} {p}: {r.message}")
        if not r.ok:
            rc = 1
    return rc


def cmd_channel(a: Any) -> int:
    from .core.channels import build_channel
    s = Settings.load()
    if a.style:
        s.forwarder_style = a.style
    drive_root = _drive_at(a.drive).root if a.drive else None
    out = Path(a.out).expanduser() if a.out else None
    info_db = None
    try:
        from .core.gametdb import TitleDB
        info_db = TitleDB.load(s)
    except Exception:  # noqa: BLE001 - titles then come from the discs
        pass
    rc = 0
    for p in a.files:
        try:
            from .core.disc import disc_info
            gid = disc_info(p).game_id
            info = info_db.get(gid) if info_db else None
            if a.wbfs_partition:
                from .core.forwarder import cover_for_channel, make_channel
                cov = cover_for_channel(gid, s.cover_lang) if s.forwarder_style == "cover" else None
                ci = make_channel(Path(p), out or s.channels_dir, style=s.forwarder_style,
                                  cover_png=cov, fat=False, title=info.title if info else None)
            else:
                ci = build_channel(Path(p), s, drive_root=drive_root,
                                   title=info.title if info else None, info=info)
                if out is not None:
                    out.mkdir(parents=True, exist_ok=True)
                    dest = out / ci.path.name
                    if dest.resolve() != ci.path.resolve():
                        # --out is often the USB drive: copy under a temp name and swap it in, so
                        # a full or unplugged drive never leaves a cut-off WAD (or loses the old one)
                        copy_atomic(ci.path, dest, fsync=True)
                        ci.path.unlink(missing_ok=True)
                        ci.path = dest
            print(f"{ci.path}  ({ci.channel_id}, {ci.style})")
        except Exception as e:  # noqa: BLE001
            if not is_user_error(e):
                raise
            _fail(p, e, out or drive_root)
            rc = 1
    return rc


def cmd_normalize(a: Any) -> int:
    from .core.games import normalize_drive
    # the same titles "games add" uses (GameTDB when available), so folder names agree
    msgs = normalize_drive(_drive(a), _titles(Settings.load()))
    for m in msgs:
        print(m)
    return 1 if any(m.startswith("skipped") for m in msgs) else 0


def cmd_migrate(a: Any) -> int:
    from .core.games import migrate_wbfs_partition
    from .core.wbfs import WbfsPartition, find_wbfs_partition
    src = Path(a.source)
    lay = _drive_at(a.drive) if a.drive else None
    off = find_wbfs_partition(src)
    if off is None:
        raise CliError(f"{src}: no WBFS partition found", 1)
    if a.list:
        part = WbfsPartition(src, off)
        try:
            for g in part.games():
                print(f"{g.slot:3}  {g.game_id}  {g.size / 1e9:6.2f} GB  {g.title}")
        finally:
            part.close()
        return 0
    s = Settings.load()
    bar = _Bar()
    try:
        res = migrate_wbfs_partition(src, lay, s, None, off, _titles(s), bar)
    finally:
        bar.done()
    print("\n".join(res) if res else "Nothing new to copy.")
    if res.problems:
        print(f"{res.skipped} skipped, {res.damaged} copied but damaged - see above.", file=sys.stderr)
        return 1
    return 0


def cmd_drives(a: Any) -> int:
    from .core.drives import list_drives
    ds = list_drives(include_fixed=a.all)
    if not ds:
        print("No removable drives found.")
    for d in ds:
        print(f"{d.display_name}\n    id={d.id}  fs={d.filesystem}  {d.partition_style}  "
              f"mount={d.mount_point or '-'}" + (f"  notes={', '.join(d.notes)}" if d.notes else ""))
    return 0


def cmd_health(a: Any) -> int:
    from .core import health
    from .core.drives import drive_for_path
    lay = _drive(a)
    drv = drive_for_path(lay.root)
    findings = health.check(lay, drv)
    for f in findings:
        print(f"[{f.severity:5}] {f.message}" + (f"  (fix: {f.fix_id})" if f.fix_id else ""))
    if not findings:
        print("Everything looks good.")
    failed = 0
    if a.fix and any(f.auto_fixable for f in findings):
        for f in findings:
            if not f.auto_fixable:
                continue
            try:
                for line in health.fix(lay, f):
                    if line.startswith(health.NOT_FIXED):
                        failed += 1
                        print(" ", line, file=sys.stderr)
                    else:
                        print("  fixed:", line)
            except Exception as e:  # noqa: BLE001
                if not is_user_error(e):
                    raise
                failed += 1
                print(f"  {health.NOT_FIXED} {_say(e, lay)}", file=sys.stderr)
        findings = health.check(lay, drv)      # the exit code describes the drive as it is now
        left = [f for f in findings if f.severity == "error"]
        print(f"After fixing: {len(findings)} finding(s) left, {len(left)} of them errors."
              + (f" {failed} fix(es) failed." if failed else ""))
    return 1 if failed or any(f.severity == "error" for f in findings) else 0


def cmd_format_drive(a: Any) -> int:
    """Non-interactive formatter; the app runs this elevated (admin/root)."""
    from .core import formatting
    from .core.drives import list_drives
    if a.confirm != a.drive_id:
        print("refusing: --confirm must equal --drive-id", file=sys.stderr)
        return 2
    drive = next((d for d in list_drives(include_fixed=a.force) if d.id == a.drive_id), None)
    if drive is None:
        print("refusing: that drive is no longer connected (or changed)", file=sys.stderr)
        return 2
    bar = _Bar()
    try:
        plan = formatting.plan_format(drive, a.label, a.cluster_size, force=a.force)
        res = formatting.execute_plan(plan, a.confirm, progress=bar)
    except (formatting.FormatRefused, formatting.FormatFailed, OSError) as e:
        bar.done()
        print(f"format failed: {e}", file=sys.stderr)
        return 1
    bar.done()
    print(f"formatted {drive.display_name}: FAT32, {res.cluster_size // 1024} KB clusters, label {res.label}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rewii", description="ReWii - all-in-one Wii toolkit. "
                                 "Run without arguments (or with just --drive PATH) for the full app.",
                                 epilog="Exit codes: 0 done, 1 failed or problems found, 2 wrong usage.")
    ap.add_argument("--version", action="version", version=f"ReWii {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info", help="show what game a file is")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_info)

    g = sub.add_parser("games", help="manage games").add_subparsers(dest="sub", required=True)
    p = g.add_parser("list", help="list games on the drive (or --library)")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--drive")
    where.add_argument("--library", action="store_true", help="list the PC library instead of the drive")
    p.set_defaults(fn=cmd_games_list)
    p = g.add_parser("add", help="add game files to the library, or to --drive")
    p.add_argument("files", nargs="+")
    p.add_argument("--drive")
    p.add_argument("--library-only", action="store_true")
    p.set_defaults(fn=cmd_games_add)
    p = g.add_parser("remove", help="remove games (by ID) from the drive")
    p.add_argument("ids", nargs="+")
    p.add_argument("--drive")
    p.add_argument("-y", "--yes", action="store_true",
                   help="don't ask before deleting (required when not run from a terminal)")
    p.set_defaults(fn=cmd_games_remove)
    p = g.add_parser("fix-layout", help="move misplaced games into the right folders")
    p.add_argument("--drive")
    p.set_defaults(fn=cmd_normalize)
    p = g.add_parser("migrate", help="copy games out of an old WBFS-formatted drive/image into the library")
    p.add_argument("source")
    p.add_argument("--drive", help="also copy them to this Wii drive")
    p.add_argument("--list", action="store_true")
    p.set_defaults(fn=cmd_migrate)

    p = sub.add_parser("drives", help="list removable drives")
    p.add_argument("--all", action="store_true", help="include fixed (non-system) disks")
    p.set_defaults(fn=cmd_drives)
    p = sub.add_parser("health", help="check the drive for problems")
    p.add_argument("--drive")
    p.add_argument("--fix", action="store_true", help="apply the safe automatic fixes")
    p.set_defaults(fn=cmd_health)
    p = sub.add_parser("format-drive", help="erase and format a drive FAT32 (needs admin)")
    p.add_argument("--drive-id", required=True)
    p.add_argument("--confirm", required=True, help="must repeat the drive id")
    p.add_argument("--label", default="REWII")
    p.add_argument("--cluster-size", type=int, default=32768)
    p.add_argument("--force", action="store_true",
                   help="allow an internal (non-USB) disk; system disks are always refused")
    p.set_defaults(fn=cmd_format_drive)

    p = sub.add_parser("verify", help="check game files for damage")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("channel", help="make a Wii Menu channel (WAD) for a game")
    p.add_argument("files", nargs="+")
    p.add_argument("--style", choices=["cover", "black", "classic"])
    p.add_argument("--out")
    p.add_argument("--wbfs-partition", action="store_true",
                   help="for old WBFS-formatted drives instead of FAT32")
    p.add_argument("--drive", help="the Wii drive (needed for GameCube games, which must be on it)")
    p.set_defaults(fn=cmd_channel)
    return ap


def main(argv: list[str]) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    try:
        return int(a.fn(a) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code
    except BrokenPipeError:
        return 1
    except Exception as e:  # noqa: BLE001 - user errors become one line; bugs still show a traceback
        if not is_user_error(e):
            raise
        print(f"error: {_say(e, getattr(a, 'drive', None) or None)}", file=sys.stderr)
        return 1
