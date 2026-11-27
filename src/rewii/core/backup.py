"""Full drive backups: snapshot, list, diff, restore and drive-to-drive clone.

Snapshots live in ``settings.drive_backups_dir/<label>/<timestamp>/`` as a
``files/`` tree plus ``manifest.json``. Incremental snapshots hard-link files
that are unchanged (same size + mtime, or same SHA-1 when hashing) since the
previous snapshot of the same label, falling back to a copy where the OS or
filesystem can't hard-link.

Nothing on a target drive is deleted unless ``mirror=True`` is passed.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Optional
from collections.abc import Iterable

from .config import Settings
from .layout import DriveLayout, safe_name
from .progress import CancelToken, ProgressFn, Reporter

MANIFEST = "manifest.json"
FILES = "files"
GAME_DIRS = ("wbfs", "games")
# OS clutter that is never backed up, compared or deleted
SKIP_NAMES = {"system volume information", "$recycle.bin", ".spotlight-v100", ".trashes",
              ".fseventsd", ".temporaryitems", "thumbs.db", ".ds_store"}
MTIME_SLACK = 2.0        # FAT32 stores mtimes with 2-second resolution
CHUNK = 4 * 1024 * 1024


class BackupError(Exception):
    """A backup/restore/clone can't proceed."""


class InsufficientSpace(BackupError):
    """The target doesn't have enough free space."""

    def __init__(self, needed: int, free: int):
        super().__init__(f"needs {needed:,} bytes but only {free:,} are free")
        self.needed = needed
        self.free = free


# --------------------------------------------------------------------------- scanning
@dataclass
class FileEntry:
    """One file in a manifest. ``path`` is relative, POSIX-style."""
    path: str
    size: int
    mtime: float
    sha1: str = ""


def _is_game_path(rel: str) -> bool:
    first = rel.split("/", 1)[0].lower()
    return first in GAME_DIRS


def _skip(name: str) -> bool:
    return name.lower() in SKIP_NAMES or name.endswith(".rewii-part")


def scan_tree(root: Path, *, include_games: bool = True,
              cancel: CancelToken | None = None) -> list[FileEntry]:
    """List regular files under ``root`` (sorted), skipping OS clutter.

    Symlinks are not followed.
    """
    root = Path(root)
    out: list[FileEntry] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if cancel is not None:
            cancel.check()
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        keep = []
        for d in dirnames:
            if _skip(d):
                continue
            if not include_games and _is_game_path(rel_dir + d):
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            if _skip(fn):
                continue
            p = Path(dirpath) / fn
            if p.is_symlink() or not p.is_file():
                continue
            st = p.stat()
            out.append(FileEntry(rel_dir + fn, st.st_size, st.st_mtime))
    out.sort(key=lambda e: e.path)
    return out


def _sha1(path: Path, rep: Reporter | None = None) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            if rep is not None:
                rep.advance(len(b))
    return h.hexdigest()


def _copy(src: Path, dest: Path, rep: Reporter, hash_: bool = False) -> str:
    """Chunked copy keeping mtime; returns SHA-1 if ``hash_``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".rewii-part")
    h = hashlib.sha1() if hash_ else None
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                b = fi.read(CHUNK)
                if not b:
                    break
                fo.write(b)
                if h is not None:
                    h.update(b)
                rep.advance(len(b))
        shutil.copystat(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return h.hexdigest() if h is not None else ""


def _same(a: FileEntry, b: FileEntry) -> bool:
    if a.size != b.size:
        return False
    if a.sha1 and b.sha1:
        return a.sha1 == b.sha1
    return abs(a.mtime - b.mtime) <= MTIME_SLACK


def _rel_to_path(base: Path, rel: str) -> Path:
    parts = PurePosixPath(rel).parts
    if any(p in ("..", "") for p in parts) or PurePosixPath(rel).is_absolute():
        raise BackupError(f"unsafe path in manifest: {rel!r}")
    return base.joinpath(*parts)


# --------------------------------------------------------------------------- snapshots
@dataclass
class Snapshot:
    """A drive snapshot stored in the library."""
    dir: Path
    label: str
    created: str
    source: str
    include_games: bool
    hashed: bool
    files: list[FileEntry] = field(default_factory=list)

    @property
    def files_dir(self) -> Path:
        return self.dir / FILES

    @property
    def size(self) -> int:
        return sum(e.size for e in self.files)

    def entry_map(self) -> dict[str, FileEntry]:
        return {e.path: e for e in self.files}


def _load_snapshot(d: Path) -> Optional[Snapshot]:
    try:
        m = json.loads((d / MANIFEST).read_text("utf-8"))
        return Snapshot(d, m["label"], m["created"], m.get("source", ""),
                        bool(m.get("include_games", True)), bool(m.get("hashed", False)),
                        [FileEntry(**e) for e in m["files"]])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def list_snapshots(settings: Settings, label: str | None = None) -> list[Snapshot]:
    """Snapshots in the library (oldest first), optionally for one label."""
    root = settings.drive_backups_dir
    out: list[Snapshot] = []
    if not root.is_dir():
        return out
    for ldir in sorted(root.iterdir()):
        if not ldir.is_dir() or (label is not None and ldir.name != safe_name(label)):
            continue
        for d in sorted(ldir.iterdir()):
            if d.is_dir() and not d.name.endswith(".partial"):
                s = _load_snapshot(d)
                if s:
                    out.append(s)
    out.sort(key=lambda s: (s.label, s.created, s.dir.name))
    return out


def create_snapshot(source: Path, settings: Settings, label: str, *,
                    include_games: bool = True, hash_files: bool = False,
                    incremental: bool = True, now: datetime | None = None,
                    progress: ProgressFn | None = None,
                    cancel: CancelToken | None = None) -> Snapshot:
    """Snapshot ``source`` (a drive root or any folder) into the library.

    ``hash_files`` stores a SHA-1 per file (slower; otherwise size+mtime is the
    change test). With ``incremental``, files unchanged since the latest
    snapshot of ``label`` are hard-linked instead of copied when possible.
    A cancelled or failed snapshot leaves nothing behind.
    """
    source = Path(source)
    if not source.is_dir():
        raise BackupError(f"{source} is not a folder")
    rep = Reporter(progress, cancel, message="Scanning drive")
    entries = scan_tree(source, include_games=include_games, cancel=cancel)
    prev = list_snapshots(settings, label)[-1:] if incremental else []
    prev_map = prev[0].entry_map() if prev else {}
    ldir = settings.drive_backups_dir / safe_name(label)
    ldir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    final = ldir / stamp
    i = 2
    while final.exists():
        final = ldir / f"{stamp}-{i}"
        i += 1
    tmp = final.with_name(final.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / FILES).mkdir(parents=True)
    rep.set_total(sum(e.size for e in entries), "Backing up drive")
    try:
        for e in entries:
            rep.check()
            src = _rel_to_path(source, e.path)
            dst = _rel_to_path(tmp / FILES, e.path)
            old = prev_map.get(e.path)
            if hash_files and old is not None and old.size == e.size:
                e.sha1 = _sha1(src)       # needed to compare with the previous snapshot
            linked = False
            if old is not None and _same(e, old):
                prev_file = _rel_to_path(prev[0].files_dir, e.path)
                if prev_file.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.link(prev_file, dst)
                        linked = True
                        e.sha1 = e.sha1 or old.sha1
                        rep.advance(e.size, e.path)
                    except OSError:
                        linked = False
            if not linked:
                rep.message = e.path
                digest = _copy(src, dst, rep, hash_=hash_files and not e.sha1)
                if digest:
                    e.sha1 = digest
        created = (now or datetime.now()).isoformat(timespec="seconds")
        manifest = {"label": label, "created": created, "source": str(source),
                    "include_games": include_games, "hashed": hash_files,
                    "files": [asdict(e) for e in entries]}
        (tmp / MANIFEST).write_text(json.dumps(manifest, indent=1), "utf-8")
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    snap = _load_snapshot(final)
    assert snap is not None
    return snap


def delete_snapshot(snapshot: Snapshot) -> None:
    """Remove a snapshot folder (hard-linked files in other snapshots survive)."""
    shutil.rmtree(snapshot.dir)


def verify_snapshot(snapshot: Snapshot, progress: ProgressFn | None = None,
                    cancel: CancelToken | None = None) -> list[str]:
    """Check every snapshot file exists with the right size (and SHA-1 if stored)."""
    rep = Reporter(progress, cancel, total=snapshot.size, message="Verifying snapshot")
    probs = []
    for e in snapshot.files:
        p = _rel_to_path(snapshot.files_dir, e.path)
        if not p.is_file():
            probs.append(f"{e.path}: missing")
            continue
        if p.stat().st_size != e.size:
            probs.append(f"{e.path}: wrong size")
        elif e.sha1 and _sha1(p, rep) != e.sha1:
            probs.append(f"{e.path}: damaged (SHA-1 mismatch)")
        elif not e.sha1:
            rep.advance(e.size)
    return probs


# --------------------------------------------------------------------------- diff
@dataclass
class SnapshotDiff:
    """Differences between a snapshot and a drive (from the drive's point of view)."""
    added: list[str] = field(default_factory=list)     # on drive, not in snapshot
    removed: list[str] = field(default_factory=list)   # in snapshot, not on drive
    changed: list[str] = field(default_factory=list)   # in both, different

    @property
    def identical(self) -> bool:
        return not (self.added or self.removed or self.changed)


def diff_snapshot(snapshot: Snapshot, drive: Path, *, deep: bool = False,
                  cancel: CancelToken | None = None) -> SnapshotDiff:
    """Compare a snapshot with a drive. ``deep`` compares SHA-1 where the snapshot has it."""
    cur = {e.path: e for e in scan_tree(Path(drive), include_games=snapshot.include_games,
                                        cancel=cancel)}
    old = snapshot.entry_map()
    d = SnapshotDiff()
    for p, e in cur.items():
        o = old.get(p)
        if o is None:
            d.added.append(p)
            continue
        if deep and o.sha1 and e.size == o.size:
            e.sha1 = _sha1(_rel_to_path(Path(drive), p))
            if e.sha1 != o.sha1:
                d.changed.append(p)
        elif not _same(e, FileEntry(o.path, o.size, o.mtime)):
            d.changed.append(p)
    d.removed = sorted(set(old) - set(cur))
    d.added.sort()
    d.changed.sort()
    return d


# --------------------------------------------------------------------------- restore / clone
@dataclass
class CopyReport:
    """Result of a restore or clone."""
    copied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)     # already identical
    deleted: list[str] = field(default_factory=list)     # only with mirror=True
    failed_verify: list[str] = field(default_factory=list)
    bytes_copied: int = 0


def _selected(rel: str, paths: Optional[Iterable[str]]) -> bool:
    if paths is None:
        return True
    for p in paths:
        p = p.strip("/").replace("\\", "/")
        if not p or rel == p or rel.startswith(p + "/"):
            return True
    return False


def _free_space(path: Path) -> int:
    p = Path(path)
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free


def _check_space(target: Path, todo: list[tuple[Path, Path, FileEntry]]) -> None:
    needed = 0
    for _, dst, e in todo:
        existing = dst.stat().st_size if dst.is_file() else 0
        needed += max(0, e.size - existing)
    free = _free_space(target)
    if needed > free:
        raise InsufficientSpace(needed, free)


def _plan(src_root: Path, entries: list[FileEntry], target: Path,
          paths: Optional[Iterable[str]], rep_skipped: list[str]) -> list[tuple[Path, Path, FileEntry]]:
    paths = list(paths) if paths is not None else None
    todo = []
    for e in entries:
        if not _selected(e.path, paths):
            continue
        dst = _rel_to_path(target, e.path)
        if dst.is_file():
            st = dst.stat()
            if _same(FileEntry(e.path, st.st_size, st.st_mtime), FileEntry(e.path, e.size, e.mtime)):
                rep_skipped.append(e.path)
                continue
        todo.append((_rel_to_path(src_root, e.path), dst, e))
    return todo


def _mirror_delete(target: Path, keep: set[str], include_games: bool,
                   paths: Optional[Iterable[str]], report: CopyReport) -> None:
    paths = list(paths) if paths is not None else None
    for e in scan_tree(target, include_games=include_games):
        if e.path not in keep and _selected(e.path, paths):
            _rel_to_path(target, e.path).unlink()
            report.deleted.append(e.path)
    # tidy empty folders left behind (never the root, never game folders when excluded)
    for dirpath, _dirs, _files in sorted(os.walk(target), key=lambda t: -len(t[0])):
        d = Path(dirpath)
        if d == target:
            continue
        rel = d.relative_to(target).as_posix()
        if (not include_games and _is_game_path(rel)) or not _selected(rel, paths):
            continue
        try:
            d.rmdir()
        except OSError:
            pass


def restore_snapshot(snapshot: Snapshot, target: Path, *, paths: Iterable[str] | None = None,
                     mirror: bool = False, verify: bool = True,
                     progress: ProgressFn | None = None,
                     cancel: CancelToken | None = None) -> CopyReport:
    """Restore all (or ``paths`` - files or folder prefixes) of a snapshot to ``target``.

    Checks free space first. Files already identical are skipped. Files on the
    target that aren't in the snapshot are only deleted when ``mirror`` is True
    (and never inside game folders the snapshot didn't include).
    """
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    report = CopyReport()
    todo = _plan(snapshot.files_dir, snapshot.files, target, paths, report.skipped)
    _check_space(target, todo)
    rep = Reporter(progress, cancel, total=sum(e.size for _, _, e in todo),
                   message="Restoring snapshot")
    for src, dst, e in todo:
        rep.check()
        if not src.is_file():
            raise BackupError(f"snapshot is missing {e.path}")
        rep.message = e.path
        digest = _copy(src, dst, rep, hash_=verify and bool(e.sha1))
        if verify and e.sha1 and digest != e.sha1:
            report.failed_verify.append(e.path)
        report.copied.append(e.path)
        report.bytes_copied += e.size
    if mirror:
        _mirror_delete(target, {e.path for e in snapshot.files}, snapshot.include_games,
                       paths, report)
    return report


def clone_drive(source: Path, target: Path, *, include_games: bool = True,
                verify: bool = True, mirror: bool = False,
                progress: ProgressFn | None = None,
                cancel: CancelToken | None = None) -> CopyReport:
    """Copy everything from drive ``source`` to drive ``target`` and verify it.

    Verification re-reads each written file and compares SHA-1 with the source.
    Files on the target not on the source are kept unless ``mirror`` is True.
    """
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise BackupError("source and target are the same drive")
    if target.resolve().is_relative_to(source.resolve()):
        raise BackupError("the target is inside the source")
    entries = scan_tree(source, include_games=include_games, cancel=cancel)
    report = CopyReport()
    todo = _plan(source, entries, target, None, report.skipped)
    _check_space(target, todo)
    total = sum(e.size for _, _, e in todo) * (2 if verify else 1)
    rep = Reporter(progress, cancel, total=total, message="Cloning drive")
    for src, dst, e in todo:
        rep.check()
        rep.message = e.path
        digest = _copy(src, dst, rep, hash_=verify)
        if verify and _sha1(dst, rep) != digest:
            report.failed_verify.append(e.path)
        report.copied.append(e.path)
        report.bytes_copied += e.size
    if mirror:
        _mirror_delete(target, {e.path for e in entries}, include_games, None, report)
    return report


def suggest_label(drive: Path) -> str:
    """A default snapshot label: the drive manifest name, else the folder/volume name."""
    lay = DriveLayout(Path(drive))
    name = lay.read_manifest().get("label") or Path(drive).resolve().name or "drive"
    return safe_name(str(name))
