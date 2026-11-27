"""Tests for rewii.core.archive (the shared safe zip extractor)."""
from __future__ import annotations

import zipfile
from pathlib import Path, PureWindowsPath

import pytest

from rewii.core import archive as A


def make(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in files.items():
            z.writestr(n, d)
    return path


@pytest.mark.parametrize("name", [
    "../evil", "a/../../evil", "/abs/file", "C:/x", "C:x", "\\\\server\\share\\x",
    "foo/D:/evil.txt",            # a drive letter in a later component moves to that drive on Windows
    "a/b:stream", "a\\..\\b", "x/con", "x/NUL.txt", "COM1/x", "lpt9.dat", "a/aux.tar.gz",
    "trail./x", "x/space ", "a/b\x01c", "a/what?.txt", "a/star*.bin",
])
def test_unsafe_names_rejected(tmp_path, name):
    with pytest.raises(A.ArchiveError):
        A.safe_parts(name)
    z = make(tmp_path / "bad.zip", {"ok/file.txt": b"1", name: b"2"})
    with pytest.raises(A.ArchiveError):
        A.extract_zip(z, tmp_path / "out")
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").rglob("*"))


def test_why_drive_letters_matter():
    # documents the Windows behaviour the per-component ':' check protects against
    assert str(PureWindowsPath("C:/Temp/x").joinpath("foo", "D:", "evil.txt")) == "D:evil.txt"


def test_safe_names_and_backslashes():
    assert A.safe_parts("apps\\x\\boot.dol") == ["apps", "x", "boot.dol"]
    assert A.safe_parts("./a//b") == ["a", "b"]
    assert A.safe_parts("dir/") is None and A.safe_parts("") is None
    assert A.safe_parts("console/contact.txt") == ["console", "contact.txt"]
    assert A.is_reserved_name("Con") and A.is_reserved_name("nul.tar.gz") and not A.is_reserved_name("icon")


def test_case_insensitive_duplicates_rejected(tmp_path):
    z = make(tmp_path / "d.zip", {"apps/x/boot.dol": b"good", "apps/x/BOOT.DOL": b"evil"})
    with pytest.raises(A.ArchiveError, match="twice"):
        A.extract_zip(z, tmp_path / "out")
    z = make(tmp_path / "e.zip", {"Apps/x/a": b"1", "apps/X/A": b"2"})
    with pytest.raises(A.ArchiveError, match="twice"):
        A.extract_zip(z, tmp_path / "out")
    z = make(tmp_path / "f.zip", {"a": b"1", "A/b": b"2"})
    with pytest.raises(A.ArchiveError, match="file and a folder"):
        A.extract_zip(z, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_size_cap(tmp_path):
    z = make(tmp_path / "bomb.zip", {"a.bin": bytes(200_000), "b.bin": bytes(200_000)})
    with pytest.raises(A.ArchiveError, match="large"):
        A.extract_zip(z, tmp_path / "out", max_total=300_000)
    assert not (tmp_path / "out").exists()
    assert len(A.extract_zip(z, tmp_path / "out", max_total=400_000).written) == 2


def test_extract_mapping_allow_and_containment(tmp_path):
    z = make(tmp_path / "ok.zip", {"w/a.txt": b"a", "w/sub/b.txt": b"b", "w/skip/c.txt": b"c", "w/d/": b""})
    out = tmp_path / "out"
    events = []
    res = A.extract_zip(z, out, mapping=lambda names: {n: n[2:] for n in names},
                        allow=lambda rel: not rel.startswith("skip/"), progress=events.append)
    assert sorted(p.relative_to(out).as_posix() for p in res.written) == ["a.txt", "sub/b.txt"]
    assert res.skipped == ["skip/c.txt"] and (out / "sub" / "b.txt").read_bytes() == b"b"
    assert events[-1].done == events[-1].total == 2 and not list(out.rglob("*.part"))
    # a mapping can't smuggle an unsafe destination in
    with pytest.raises(A.ArchiveError):
        A.extract_zip(z, out, mapping=lambda names: {n: "../" + n for n in names})
    # a symlinked folder inside the destination can't redirect files outside it
    outside = tmp_path / "outside"
    outside.mkdir()
    (out / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(A.ArchiveError):
        A.extract_zip(make(tmp_path / "l.zip", {"link/x.txt": b"x"}), out)
    assert not any(outside.iterdir())
    with pytest.raises(A.ArchiveError, match="not a valid zip"):
        A.extract_zip(tmp_path / "ok.zip.missing", out)
