"""Tests for rewii.core.backup."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from rewii.core import backup as B
from rewii.core.config import Settings
from rewii.core.progress import CancelToken, Cancelled


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(library_dir=str(tmp_path / "lib"))


def make_drive(root: Path) -> Path:
    files = {
        "apps/usbloader_gx/boot.dol": b"dol" * 100,
        "apps/usbloader_gx/meta.xml": b"<app/>",
        "wbfs/Game [RMCE01]/RMCE01.wbfs": b"W" * 5000,
        "games/Melee [GALE01]/game.iso": b"G" * 3000,
        "saves/GALE.raw": b"S" * 200,
        "System Volume Information/junk": b"x",
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def test_scan_tree_excludes(tmp_path: Path) -> None:
    make_drive(tmp_path)
    all_ = [e.path for e in B.scan_tree(tmp_path)]
    assert "wbfs/Game [RMCE01]/RMCE01.wbfs" in all_
    assert not any(p.startswith("System Volume") for p in all_)
    no_games = [e.path for e in B.scan_tree(tmp_path, include_games=False)]
    assert no_games == ["apps/usbloader_gx/boot.dol", "apps/usbloader_gx/meta.xml",
                        "saves/GALE.raw"]


def test_snapshot_incremental_hardlinks(tmp_path: Path, settings: Settings) -> None:
    drive = make_drive(tmp_path / "drive")
    s1 = B.create_snapshot(drive, settings, "My USB", include_games=False,
                           now=datetime(2026, 1, 1))
    assert s1.dir.name == "20260101-000000" and len(s1.files) == 3
    assert (s1.files_dir / "saves" / "GALE.raw").read_bytes() == b"S" * 200
    (drive / "saves" / "GALE.raw").write_bytes(b"T" * 300)
    s2 = B.create_snapshot(drive, settings, "My USB", include_games=False, hash_files=True,
                           now=datetime(2026, 1, 2))
    dol1 = s1.files_dir / "apps/usbloader_gx/boot.dol"
    dol2 = s2.files_dir / "apps/usbloader_gx/boot.dol"
    assert os.path.samefile(dol1, dol2)                       # unchanged -> hard link
    assert not os.path.samefile(s1.files_dir / "saves/GALE.raw", s2.files_dir / "saves/GALE.raw")
    assert all(e.sha1 for e in s2.files)
    assert [s.dir for s in B.list_snapshots(settings, "My USB")] == [s1.dir, s2.dir]
    assert B.verify_snapshot(s2) == []


def test_diff_and_restore(tmp_path: Path, settings: Settings) -> None:
    drive = make_drive(tmp_path / "drive")
    snap = B.create_snapshot(drive, settings, "usb", include_games=True, hash_files=True)
    (drive / "saves" / "GALE.raw").write_bytes(b"changed!")
    (drive / "apps" / "usbloader_gx" / "meta.xml").unlink()
    (drive / "new.txt").write_text("new")
    d = B.diff_snapshot(snap, drive)
    assert d.added == ["new.txt"]
    assert d.removed == ["apps/usbloader_gx/meta.xml"]
    assert d.changed == ["saves/GALE.raw"]

    rep = B.restore_snapshot(snap, drive, paths=["saves"])
    assert rep.copied == ["saves/GALE.raw"]
    assert (drive / "saves" / "GALE.raw").read_bytes() == b"S" * 200
    assert (drive / "new.txt").exists()                       # never deleted without mirror

    rep = B.restore_snapshot(snap, drive, mirror=True)
    assert rep.copied == ["apps/usbloader_gx/meta.xml"]
    assert rep.deleted == ["new.txt"] and not rep.failed_verify
    assert B.diff_snapshot(snap, drive, deep=True).identical


def test_mirror_keeps_games_when_snapshot_excluded_them(tmp_path: Path, settings: Settings) -> None:
    drive = make_drive(tmp_path / "drive")
    snap = B.create_snapshot(drive, settings, "usb", include_games=False)
    B.restore_snapshot(snap, drive, mirror=True)
    assert (drive / "wbfs" / "Game [RMCE01]" / "RMCE01.wbfs").exists()


def test_restore_space_check(tmp_path: Path, settings: Settings, monkeypatch) -> None:
    drive = make_drive(tmp_path / "drive")
    snap = B.create_snapshot(drive, settings, "usb")
    monkeypatch.setattr(B, "_free_space", lambda p: 10)
    with pytest.raises(B.InsufficientSpace):
        (tmp_path / "empty").mkdir()
        B.restore_snapshot(snap, tmp_path / "empty")


def test_clone_and_cancel(tmp_path: Path, settings: Settings) -> None:
    src = make_drive(tmp_path / "a")
    dst = tmp_path / "b"
    (dst / "keep.txt").parent.mkdir(parents=True)
    (dst / "keep.txt").write_text("mine")
    rep = B.clone_drive(src, dst)
    assert len(rep.copied) == 5 and not rep.failed_verify
    assert (dst / "keep.txt").exists()
    assert (dst / "games/Melee [GALE01]/game.iso").read_bytes() == b"G" * 3000
    rep2 = B.clone_drive(src, dst)
    assert rep2.copied == [] and len(rep2.skipped) == 5
    with pytest.raises(B.BackupError):
        B.clone_drive(src, src / "apps")

    tok = CancelToken()
    tok.cancel()
    with pytest.raises(Cancelled):
        B.create_snapshot(src, settings, "c", cancel=tok)
    assert B.list_snapshots(settings, "c") == []
    assert not any((settings.drive_backups_dir / "c").glob("*.partial"))


def test_safe_join_rejects_windows_escapes(tmp_path):
    from rewii.core.fileops import UnsafePath, safe_join
    for bad in ("../x", "a/../../x", "/abs", "C:/Windows/x", "..\\..\\x", "a\\b", ""):
        with pytest.raises(UnsafePath):
            safe_join(tmp_path, bad)
    assert safe_join(tmp_path, "apps/x/boot.dol") == tmp_path / "apps" / "x" / "boot.dol"


def test_mirror_delete_is_case_insensitive(tmp_path):
    from rewii.core import backup as BK
    from rewii.core.backup import CopyReport
    (tmp_path / "apps" / "wiixplorer").mkdir(parents=True)
    (tmp_path / "apps" / "wiixplorer" / "boot.dol").write_bytes(b"x")
    rep = CopyReport()
    BK._mirror_delete(tmp_path, {"apps/WiiXplorer/boot.dol"}, True, None, rep)
    assert (tmp_path / "apps" / "wiixplorer" / "boot.dol").exists() and not rep.deleted


def test_clone_refuses_overlap(tmp_path):
    from rewii.core import backup as BK
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f").write_bytes(b"x")
    with pytest.raises(BK.BackupError):
        BK.clone_drive(tmp_path / "sub", tmp_path, mirror=True)
    assert (tmp_path / "sub" / "f").exists()


def test_snapshot_skips_library_inside_source(tmp_path):
    from rewii.core import backup as BK
    from rewii.core.config import Settings
    src = tmp_path / "drive"
    (src / "apps").mkdir(parents=True)
    (src / "apps" / "a.txt").write_bytes(b"1")
    s = Settings(library_dir=str(src / "ReWii"))
    BK.create_snapshot(src, s, "self")
    snap = BK.create_snapshot(src, s, "self")
    assert [e.path for e in snap.files] == ["apps/a.txt"]
