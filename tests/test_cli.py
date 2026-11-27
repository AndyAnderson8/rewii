"""Command line: exit codes and error paths. User mistakes give one ``error:`` line, never a traceback."""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from fakedisc import make_wii_iso
from rewii import __main__ as entry
from rewii import cli
from rewii.core.config import Settings


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    s = Settings(library_dir=str(tmp_path / "lib"))
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, path=None: s))
    monkeypatch.setattr(cli, "_title_db", lambda settings: None)    # no GameTDB download
    monkeypatch.setattr("sys.stdin", io.StringIO(""))               # not a terminal
    return s


@pytest.fixture
def drive(tmp_path) -> Path:
    d = tmp_path / "drive"
    f = d / "wbfs" / "Alpha [RAAE01]" / "RAAE01.iso"
    f.parent.mkdir(parents=True)
    make_wii_iso(f, "RAAE01", "Alpha")
    return d


def run(capsys, *argv: str) -> tuple[int, str, str]:
    try:
        rc = entry.main(list(argv))
    except SystemExit as e:                     # argparse usage errors
        rc = e.code if isinstance(e.code, int) else 1
    out = capsys.readouterr()
    assert "Traceback" not in out.err
    return rc, out.out, out.err


def test_missing_files_are_one_line_errors(capsys, tmp_path, drive):
    missing = str(tmp_path / "nope.iso")
    for argv in (["info", missing], ["games", "add", missing], ["games", "add", missing, "--drive", str(drive)],
                 ["channel", missing], ["games", "migrate", missing], ["verify", missing]):
        rc, out, err = run(capsys, *argv)
        assert rc == 1, argv
        assert "nope.iso" in err + out, argv


def test_not_a_game_and_no_wbfs_partition(capsys, tmp_path):
    junk = tmp_path / "junk.iso"
    junk.write_bytes(b"\0" * 4096)
    assert run(capsys, "info", str(junk))[0] == 1
    assert run(capsys, "games", "add", str(junk))[0] == 1
    rc, _out, err = run(capsys, "games", "migrate", str(junk))
    assert rc == 1 and "no WBFS partition" in err


def test_info_ok(capsys, drive):
    rc, out, _ = run(capsys, "info", str(drive / "wbfs" / "Alpha [RAAE01]" / "RAAE01.iso"))
    assert rc == 0 and "RAAE01" in out


def test_drive_must_be_given_and_exist(capsys, tmp_path, drive):
    assert run(capsys, "games", "list")[0] == 2                              # none given, none remembered
    for argv in (["games", "list", "--drive", str(tmp_path / "gone")], ["health", "--drive", str(tmp_path / "gone")],
                 ["games", "fix-layout", "--drive", str(tmp_path / "gone")],
                 ["games", "add", "x.iso", "--drive", str(tmp_path / "gone")],
                 ["games", "remove", "RAAE01", "-y", "--drive", str(tmp_path / "gone")]):
        rc, _out, err = run(capsys, *argv)
        assert rc == 2 and "isn't there" in err, argv
    rc, out, _ = run(capsys, "games", "list", "--drive", str(drive))
    assert rc == 0 and "RAAE01" in out
    assert run(capsys, "games", "list", "--library")[0] == 0                 # needs no drive
    assert run(capsys, "games", "list", "--library", "--drive", str(drive))[0] == 2


def test_remove_needs_yes_without_a_terminal(capsys, drive):
    game = drive / "wbfs" / "Alpha [RAAE01]" / "RAAE01.iso"
    rc, _out, err = run(capsys, "games", "remove", "RAAE01", "--drive", str(drive))
    assert rc == 2 and "-y" in err and game.exists()
    rc, _out, err = run(capsys, "games", "remove", "ZZZZ99", "-y", "--drive", str(drive))
    assert rc == 1 and "not on the drive" in err and game.exists()
    rc, out, _ = run(capsys, "games", "remove", "raae01", "-y", "--drive", str(drive))
    assert rc == 0 and "removed" in out and not game.exists()


def test_remove_asks_on_a_terminal(capsys, drive, monkeypatch):
    game = drive / "wbfs" / "Alpha [RAAE01]" / "RAAE01.iso"

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True
    monkeypatch.setattr("sys.stdin", Tty("n\n"))
    assert run(capsys, "games", "remove", "RAAE01", "--drive", str(drive))[0] == 0
    assert game.exists()
    monkeypatch.setattr("sys.stdin", Tty("y\n"))
    assert run(capsys, "games", "remove", "RAAE01", "--drive", str(drive))[0] == 0
    assert not game.exists()


def test_verify_exit_code(capsys, drive, tmp_path):
    assert run(capsys, "verify", str(drive / "wbfs" / "Alpha [RAAE01]" / "RAAE01.iso"))[0] == 0
    junk = tmp_path / "junk.iso"
    junk.write_bytes(b"\0" * 4096)
    assert run(capsys, "verify", str(junk))[0] == 1


def test_health_on_a_real_folder(capsys, drive):
    rc, out, _ = run(capsys, "health", "--drive", str(drive), "--fix")
    assert rc in (0, 1) and out


def test_channel_out_is_moved_even_across_filesystems(capsys, tmp_path, monkeypatch, drive):
    from rewii.core import channels, disc
    from rewii.core.forwarder import ChannelInfo
    made = tmp_path / "lib" / "Channels" / "Alpha [RAAE01].wad"

    def fake_build(path, settings, **kw):
        made.parent.mkdir(parents=True, exist_ok=True)
        made.write_bytes(b"wad")
        return ChannelInfo("RAAE01", "Alpha", "UAAE", "black", made, 3)

    def no_rename(self, target):                 # what happens when --out is on another disk
        raise OSError(18, "Invalid cross-device link")
    monkeypatch.setattr(channels, "build_channel", fake_build)
    monkeypatch.setattr(Path, "replace", no_rename)
    monkeypatch.setattr(Path, "rename", no_rename)
    monkeypatch.setattr("os.rename", lambda *a, **k: no_rename(None, None))
    assert disc.disc_info                         # the real header reader is used
    out_dir = tmp_path / "usb" / "wad"
    rc, out, _ = run(capsys, "channel", str(drive / "wbfs" / "Alpha [RAAE01]" / "RAAE01.iso"),
                     "--style", "black", "--out", str(out_dir))
    assert rc == 0 and (out_dir / made.name).read_bytes() == b"wad" and not made.exists()
    assert str(out_dir / made.name) in out


def test_bugs_are_not_swallowed(monkeypatch, drive):
    from rewii.core import games

    def bug(*a, **k):
        raise RuntimeError("a real bug")
    monkeypatch.setattr(games, "scan_drive", bug)
    with pytest.raises(RuntimeError):
        entry.main(["games", "list", "--drive", str(drive)])


def test_is_user_error():
    from rewii.core.games import GameError
    assert cli.is_user_error(FileNotFoundError(2, "x")) and cli.is_user_error(GameError("x"))
    assert not cli.is_user_error(RuntimeError("x")) and not cli.is_user_error(KeyError("x"))


# --------------------------------------------------------------------------- opening the app
def test_app_drive_forms(capsys, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr("rewii.tui.app.run", lambda drive=None: opened.append(drive))
    assert entry.main([]) == 0
    assert entry.main(["--drive", str(tmp_path)]) == 0
    assert entry.main([f"--drive={tmp_path}"]) == 0
    assert opened == [None, tmp_path, tmp_path]
    for argv in (["--drive"], ["--drive="], ["--drive", str(tmp_path / "gone")], [f"--drive={tmp_path / 'gone'}"],
                 ["--drive", str(tmp_path), "games", "list"]):
        rc, _out, err = run(capsys, *argv)
        assert rc == 2 and err.startswith("error:"), argv
    assert len(opened) == 3


def test_setup_command_uses_the_core_setup(drive, capsys, monkeypatch, _isolated):
    from rewii.core import osc
    calls = []

    def fake_kit(layout, platform="", which="essential", progress=None, cancel=None, **k):
        calls.append((layout.root, platform, which))
        return osc.StarterKitReport(installed=["usbloader_gx"])
    monkeypatch.setattr(osc, "install_starter_kit", fake_kit)
    assert entry.main(["setup", "--drive", str(drive)]) == 2            # asks for -y, changes nothing
    assert calls == [] and not (drive / "rewii").exists()
    assert "usbloader_gx" in capsys.readouterr().out
    _isolated.console_type = "vwii"
    assert entry.main(["setup", "--drive", str(drive), "-y"]) == 0
    out = capsys.readouterr().out
    assert calls == [(drive, "vwii", "essential")] and "Installed: usbloader_gx" in out and "Next:" in out
    assert (drive / "rewii" / "drive.json").is_file() and (drive / "apps").is_dir() and (drive / "wad").is_dir()
    assert entry.main(["setup", "--drive", str(drive / "gone"), "-y"]) == 2


def test_selftest_is_hidden_and_passes(capsys):
    assert entry.main(["selftest"]) == 0
    out = capsys.readouterr().out
    assert out.count("ok  ") == 4 and "FAIL" not in out and "selftest passed" in out
    with pytest.raises(SystemExit):
        entry.main(["--help"])
    assert "selftest" not in capsys.readouterr().out


def test_selftest_reports_a_missing_bundled_file(capsys, monkeypatch):
    from rewii.core import selftest
    monkeypatch.setattr(selftest, "BUNDLED_ASSETS", (*selftest.BUNDLED_ASSETS, "not_shipped.bin"))
    assert entry.main(["selftest"]) == 1
    cap = capsys.readouterr()
    assert "FAIL  bundled files" in cap.out and "not_shipped.bin" in cap.out and "FAILED" in cap.err


def test_version_comes_from_the_package_metadata():
    import tomllib
    import rewii
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text("utf-8"))
    assert rewii.__version__ == pyproject["project"]["version"]
