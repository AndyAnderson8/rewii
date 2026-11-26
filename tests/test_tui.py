"""Headless tests for the terminal app (Textual pilot). No network, no real drives."""
from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest
from textual.widgets import Button, Input, Select, SelectionList

from fakedisc import make_wii_iso
from rewii.core import backup as BK
from rewii.core import drives as DR
from rewii.core import games as G
from rewii.core import osc
from rewii.core import wad as W
from rewii.core.cheats import Cheat
from rewii.core.config import Settings
from rewii.tui import widgets as Wd
from rewii.tui.app import ReWiiApp
from rewii.tui.base import FAILED
from rewii.tui.panes.games import build_rows

SIZE = (160, 50)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Settings/cache in the temp dir, no background downloads, no sockets, no real drives."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(Settings, "path", classmethod(lambda cls: tmp_path / "cfg" / "settings.json"))
    monkeypatch.setattr(ReWiiApp, "load_titles", lambda self: None)
    monkeypatch.setattr(ReWiiApp, "check_update", lambda self: None)
    monkeypatch.setattr(osc, "fetch_index", lambda refresh=False: [])
    monkeypatch.setattr(DR, "list_drives", lambda *a, **k: [])

    def no_net(self, *a, **k):
        raise OSError("network is blocked in tests")
    monkeypatch.setattr(socket.socket, "connect", no_net)


def make_app(tmp_path: Path, drive: bool = True, **settings) -> ReWiiApp:
    (tmp_path / "drive").mkdir(exist_ok=True)
    s = Settings(library_dir=str(tmp_path / "lib"), first_run_done=True, **settings)
    return ReWiiApp(settings=s, drive=(tmp_path / "drive") if drive else None)


async def settle(pilot, n: int = 4) -> None:
    """Let workers/threads make progress, then wait for the screen to catch up (once)."""
    for _ in range(n):
        await asyncio.sleep(0.01)
    await pilot.pause()


async def until(pilot, cond, what: str = "condition", tries: int = 200) -> None:
    for _ in range(tries * 2):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def dialog(pilot, kind=None):
    """Wait for a modal dialog (optionally of a given class) and return it."""
    app = pilot.app
    await until(pilot, lambda: len(app.screen_stack) > 1 and (kind is None or isinstance(app.screen, kind)),
                f"dialog {kind.__name__ if kind else ''}")
    await settle(pilot, 2)
    return app.screen


async def closed(pilot) -> None:
    await until(pilot, lambda: len(pilot.app.screen_stack) == 1, "dialogs to close")


def drive_game(tmp_path: Path, gid: str, title: str) -> Path:
    f = tmp_path / "drive" / "wbfs" / f"{title} [{gid}]" / f"{gid}.iso"
    f.parent.mkdir(parents=True)
    make_wii_iso(f, gid, title)
    return f


async def open_games(pilot, tmp_path, n_rows: int):
    app = pilot.app
    app.action_section("games")
    pane = app.pane("games")
    await until(pilot, lambda: pane.table.row_count == n_rows, f"{n_rows} game rows")
    await settle(pilot)
    return pane


# --------------------------------------------------------------------------- marking / selection
async def test_ticked_rows_hidden_by_the_filter_are_not_acted_on(tmp_path):
    files = {t: drive_game(tmp_path, gid, t) for gid, t in
             (("RAAE01", "Alpha"), ("RBBE01", "Beta"), ("RCCE01", "Gamma"))}
    app = make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        pane = await open_games(pilot, tmp_path, 3)
        assert app.focused is pane.table          # shortcuts work right after switching section
        await pilot.press("space", "space")
        assert pane.table.marked == {"RAAE01:0", "RBBE01:0"}
        assert [r.any.title for r in pane._selected()] == ["Alpha", "Beta"]   # display order

        pane.query_one("#filter", Input).value = "gamma"
        await settle(pilot)
        assert pane.table.row_count == 1
        assert pane.table.marked == set()
        assert [r.any.title for r in pane._selected()] == ["Gamma"]

        pane.query_one("#remove", Button).press()
        dlg = await dialog(pilot, Wd.Confirm)
        assert "Delete 1 game(s)" in dlg.body and "Gamma" in dlg.body     # titles are always listed
        assert "Alpha" not in dlg.body and "Beta" not in dlg.body
        dlg.query_one("#yes", Button).press()
        await until(pilot, lambda: not files["Gamma"].exists(), "Gamma to be deleted")
        await closed(pilot)
    assert files["Alpha"].exists() and files["Beta"].exists()
    assert app.return_code in (None, 0)


async def test_marktable_selection_is_marked_and_visible_in_display_order(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=SIZE):
        t = Wd.MarkTable()
        await app.mount(t)
        t.set_columns("Name")
        t.fill([("a", ["A"]), ("b", ["B"]), ("c", ["C"])])
        assert t.selected_keys() == ["a"]            # nothing ticked: the highlighted row
        t.marked = {"c", "a", "gone"}
        assert t.selected_keys() == ["a", "c"]
        t.fill([("c", ["C"]), ("b", ["B"])])         # "a" is filtered out
        assert t.marked == {"c"} and t.selected_keys() == ["c"]
        t.clear_marks()
        t.fill([])
        assert t.selected_keys() == []


def _game(gid: str, path: str, loc: str = "library", title: str = "T") -> G.Game:
    return G.Game(gid, title, title, "wii", 0, "NTSC-U", "ISO", Path(path), [Path(path)], 1, loc)


def test_duplicate_copies_get_their_own_rows():
    lib = [_game("RAAE01", "/l/a.iso"), _game("RAAE01", "/l/a.wbfs"), _game("??????", "/l/x.rvz"),
           _game("??????", "/l/y.rvz")]
    drv = [_game("RAAE01", "/d/a.wbfs", "drive"), _game("RBBE01", "/d/b.wbfs", "drive")]
    rows = build_rows(lib, drv)
    assert len({r.key for r in rows}) == len(rows) == 5
    a = [r for r in rows if r.any.game_id == "RAAE01"]
    assert len(a) == 2 and all(r.duplicate for r in a)
    assert {r.lib.path.name for r in a if r.lib} == {"a.iso", "a.wbfs"}     # each row = the file it shows
    assert sum(1 for r in a if r.drive) == 1
    unknown = [r for r in rows if r.any.game_id == "??????"]
    assert len(unknown) == 2 and not any(r.duplicate for r in unknown)
    assert not next(r for r in rows if r.any.game_id == "RBBE01").duplicate


def test_channel_id_collision_warning():
    from rewii.core import channels as CH
    from rewii.tui.panes._channels import collision_warning
    if not hasattr(CH, "channel_id_collisions"):
        pytest.skip("core has no channel_id_collisions yet")
    a, b, c = (_game("RMCE01", "/d/a", "drive", "Kart"), _game("SMCE01", "/d/b", "drive", "Other"),
               _game("RZZE01", "/d/c", "drive", "Lonely"))
    w = collision_warning([a], [a, b, c])
    assert "Kart" in w and "Other" in w and "Lonely" not in w
    assert collision_warning([c], [a, b, c]) == ""       # a clash between games not being made is not shown
    assert collision_warning([c], []) == ""


async def test_delete_failure_is_reported_not_fatal(tmp_path, monkeypatch):
    drive_game(tmp_path, "RAAE01", "Alpha")

    def boom(layout, game):
        raise PermissionError(13, "Permission denied", str(game.path))
    monkeypatch.setattr(G, "remove_game", boom)
    app = make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        pane = await open_games(pilot, tmp_path, 1)
        await pilot.press("space")
        pane.action_remove()
        (await dialog(pilot, Wd.Confirm)).query_one("#yes", Button).press()
        msg = await dialog(pilot, Wd.Message)
        assert "Permission denied" in msg.body
        assert app.is_running
        await pilot.press("escape")
        await closed(pilot)
    assert app.return_code in (None, 0)


# --------------------------------------------------------------------------- scans
async def test_scan_thread_exception_does_not_exit_the_app(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise PermissionError(13, "Permission denied", "lib")
    monkeypatch.setattr(G, "scan_library", boom)
    monkeypatch.setattr(W, "scan_wads", boom)
    monkeypatch.setattr(BK, "list_snapshots", boom)
    app = make_app(tmp_path)
    async with app.run_test(size=SIZE, notifications=True) as pilot:
        for key in ("games", "channels", "backups", "saves", "drive", "homebrew"):
            app.action_section(key)
            await settle(pilot, 8)
        await until(pilot, lambda: any("failed" in n.message for n in app._notifications), "an error notice")
        assert app.is_running
    assert app.return_code in (None, 0)


async def test_games_are_read_again_when_titles_arrive(tmp_path):
    drive_game(tmp_path, "RAAE01", "Alpha")
    app = make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        pane = await open_games(pilot, tmp_path, 1)
        app.title_lookup = lambda gid: "Official Alpha"      # the database "arrives"
        app._titles_ready()
        await until(pilot, lambda: pane.rows["RAAE01:0"].any.title == "Official Alpha", "rescan with titles")


async def test_unusable_library_does_not_stop_the_app_from_starting(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    app = ReWiiApp(settings=Settings(library_dir=str(blocker / "lib"), first_run_done=True))
    assert app.startup_problems
    async with app.run_test(size=SIZE, notifications=True) as pilot:
        for key in ("games", "channels", "saves", "backups", "settings"):
            app.action_section(key)
            await settle(pilot, 6)
        assert app.is_running
    assert app.return_code in (None, 0)


# --------------------------------------------------------------------------- dialogs
async def test_confirm_returns_none_on_escape_and_choice_has_cancel(tmp_path):
    app = make_app(tmp_path)
    got = []
    async with app.run_test(size=SIZE) as pilot:
        app.push_screen(Wd.Confirm("t", "b"), got.append)
        await dialog(pilot, Wd.Confirm)
        await pilot.press("escape")
        await closed(pilot)
        app.push_screen(Wd.Confirm("t", "b"), got.append)
        (await dialog(pilot, Wd.Confirm)).query_one("#no", Button).press()
        await closed(pilot)
        app.push_screen(Wd.Choice("t", "b", "First", "Second"), got.append)
        await dialog(pilot, Wd.Choice)
        await pilot.press("escape")
        await closed(pilot)
        app.push_screen(Wd.Choice("t", "b", "First", "Second"), got.append)
        (await dialog(pilot, Wd.Choice)).query_one("#b", Button).press()
        await closed(pilot)
    assert got == [None, False, None, "b"]


async def test_escape_on_either_or_prompts_cancels_the_operation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(osc, "install_starter_kit", lambda *a, **k: calls.append("kit"))
    monkeypatch.setattr(BK, "create_snapshot", lambda *a, **k: calls.append("snapshot"))
    app = make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        app.action_section("homebrew")
        await settle(pilot)
        app.pane("homebrew").query_one("#kit", Button).press()
        await dialog(pilot, Wd.Choice)
        await pilot.press("escape")
        await closed(pilot)
        app.action_section("backups")
        await settle(pilot)
        app.pane("backups").query_one("#snap", Button).press()
        await dialog(pilot, Wd.Choice)
        await pilot.press("escape")
        await closed(pilot)
        await settle(pilot)
    assert calls == []


async def test_removing_wads_from_the_drive_asks_first(tmp_path, monkeypatch):
    info = W.WadInfo(path=tmp_path / "drive" / "wad" / "Game.wad", title_id="0001000146424345",
                     title_ascii="FBCE", title_type="Channel", name="Some Channel", region="NTSC-U",
                     id_region="NTSC-U", version=1, required_ios=58, file_size=10, installed_size=10,
                     num_contents=1, common_key_index=0, platform="wii", signed=True,
                     tmd_fakesigned=False, ticket_fakesigned=False, hashes_ok=None)
    monkeypatch.setattr(W, "scan_wads", lambda folder, **k: W.ScanResult())
    monkeypatch.setattr(W, "list_staged", lambda layout: W.ScanResult([info]))
    removed = []
    monkeypatch.setattr(W, "unstage", lambda layout, items: removed.extend(items) or list(items))
    app = make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        app.action_section("channels")
        pane = app.pane("channels")
        await until(pilot, lambda: pane.table.row_count == 1, "the staged WAD")
        pane.query_one("#unstage", Button).press()
        dlg = await dialog(pilot, Wd.Confirm)
        assert "Some Channel" in dlg.body and "no copy on this computer" in dlg.body
        await pilot.press("escape")
        await closed(pilot)
        assert removed == []
        pane.query_one("#unstage", Button).press()
        (await dialog(pilot, Wd.Confirm)).query_one("#yes", Button).press()
        await until(pilot, lambda: removed == [info.path], "the WAD to be removed")
        await closed(pilot)


async def test_job_tells_failure_apart_from_a_none_result(tmp_path):
    app = make_app(tmp_path)
    out = {}
    async with app.run_test(size=SIZE) as pilot:
        pane = app.pane("home")

        async def go():
            out["none"] = await pane.job("Quiet work", lambda p, c: None, quiet=True)
            out["bad"] = await pane.job("Broken work", lambda p, c: 1 / 0)
            out["bg"] = await pane.bg("Reading", lambda: {}["missing"])
        task = asyncio.ensure_future(app.run_worker(go()).wait())
        msg = await dialog(pilot, Wd.Message)
        assert "division by zero" in msg.body
        await pilot.press("escape")
        await until(pilot, lambda: len(app.screen_stack) > 1 and app.screen is not msg, "second error")
        await pilot.press("escape")
        await task
    assert out["none"] is None and out["bad"] is FAILED and out["bg"] is FAILED and not FAILED


# --------------------------------------------------------------------------- cheat picker
async def test_cheatpicker_two_placeholder_cheats_keep_typed_values(tmp_path):
    cheats = [Cheat("Inf lives", [("04123456", "000000XX")]), Cheat("Speed", [("04123460", "0000YYYY")]),
              Cheat("Plain", [("04123470", "00000001")])]
    app = make_app(tmp_path)
    got = []
    async with app.run_test(size=SIZE) as pilot:
        scr = Wd.CheatPicker("Game", cheats, [])
        app.push_screen(scr, got.append)
        await dialog(pilot, Wd.CheatPicker)
        sl = scr.query_one(SelectionList)
        sl.select(0)
        await until(pilot, lambda: len(scr.query(Input)) == 1, "first value box")
        scr.query(Input).first().value = "63"
        sl.select(1)                                   # used to crash with DuplicateIds
        await until(pilot, lambda: len(scr.query(Input)) == 2, "second value box")
        boxes = list(scr.query(Input))
        assert boxes[0].value == "63"                  # what was typed is kept
        boxes[1].value = "1F4"
        sl.select(2)
        await settle(pilot)
        assert [b.value for b in scr.query(Input)] == ["63", "1F4"]
        scr.query_one("#save", Button).press()
        await closed(pilot)
    assert got == [([0, 1, 2], {"Inf lives": {"X": "63"}, "Speed": {"Y": "1F4"}})]
    assert app.return_code in (None, 0)


# --------------------------------------------------------------------------- settings
async def test_settings_save_keeps_a_profile_set_elsewhere(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        app.action_section("settings")
        await settle(pilot)
        # what "Read SysCheck report" and "Send to Wii" do while Settings is not showing
        app.action_section("system")
        app.settings.console_type, app.settings.console_region = "wii", "PAL"
        app.settings.wii_ip = "192.168.1.50"
        app.action_section("settings")
        await settle(pilot)
        pane = app.pane("settings")
        assert pane.query_one("#ctype", Select).value == "wii"
        pane.query_one("#lang", Select).value = "FR"
        pane.query_one("#save", Button).press()
        await settle(pilot)
        s = app.settings
        assert (s.console_type, s.console_region, s.cover_lang, s.wii_ip) == ("wii", "PAL", "FR", "192.168.1.50")
        saved = Settings.load()
        assert (saved.console_type, saved.console_region, saved.cover_lang) == ("wii", "PAL", "FR")


async def test_settings_rejects_empty_and_relative_library(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        app.action_section("settings")
        await settle(pilot)
        pane = app.pane("settings")
        before = app.settings.library_dir
        for bad in ("", "   ", "games/here", "."):
            pane.query_one("#library", Input).value = bad
            pane.query_one("#lang", Select).value = "DE"
            pane.query_one("#save", Button).press()
            await settle(pilot)
            assert app.settings.library_dir == before
            assert app.settings.cover_lang == "EN"       # nothing is applied when a value is invalid
        new = tmp_path / "newlib"
        pane.query_one("#library", Input).value = str(new)
        pane.query_one("#save", Button).press()
        await settle(pilot)
        assert app.settings.library_dir == str(new) and (new / "Games" / "Wii").is_dir()


# --------------------------------------------------------------------------- file picker
async def test_pickpath_space_ticks_files(tmp_path):
    pick = tmp_path / "pick"
    pick.mkdir()
    for n in ("a.wad", "b.wad", "c.txt"):
        (pick / n).write_bytes(b"x")
    app = make_app(tmp_path)
    got = []
    async with app.run_test(size=SIZE) as pilot:
        scr = Wd.PickPath("Pick", start=pick, exts={".wad"}, multiple=True)
        app.push_screen(scr, got.append)
        await dialog(pilot, Wd.PickPath)
        tree = scr.query_one("#tree")
        await until(pilot, lambda: len(tree.root.children) == 2, "the two .wad files")
        assert app.focused is tree
        await pilot.press("down", "space", "space")
        assert [p.name for p in scr.marked] == ["a.wad", "b.wad"]
        await pilot.press("up", "space")                # cursor is back on a.wad: untick it
        assert [p.name for p in scr.marked] == ["b.wad"]
        scr.query_one("#select", Button).press()
        await closed(pilot)
    assert got == [[pick / "b.wad"]]


async def test_pickpath_any_path_takes_a_typed_device_and_either_takes_a_folder(tmp_path):
    app = make_app(tmp_path)
    got = []
    async with app.run_test(size=SIZE) as pilot:
        scr = Wd.PickPath("Device", start=tmp_path)             # plain file mode refuses a device
        app.push_screen(scr, got.append)
        await dialog(pilot, Wd.PickPath)
        scr.query_one("#path", Input).value = "/dev/null"
        scr.query_one("#select", Button).press()
        await settle(pilot)
        assert app.screen is scr
        await pilot.press("escape")
        await closed(pilot)

        scr = Wd.PickPath("Device", start=tmp_path, any_path=True)
        app.push_screen(scr, got.append)
        await dialog(pilot, Wd.PickPath)
        scr.query_one("#path", Input).value = "/dev/null"
        scr.query_one("#select", Button).press()
        await closed(pilot)

        scr = Wd.PickPath("Mod", start=tmp_path, exts={".zip"}, either=True)
        app.push_screen(scr, got.append)
        await dialog(pilot, Wd.PickPath)
        scr.query_one("#path", Input).value = str(tmp_path / "drive")
        scr.query_one("#select", Button).press()
        await closed(pilot)
    assert got == [None, [Path("/dev/null")], [tmp_path / "drive"]]


# --------------------------------------------------------------------------- smoke
GROUPS = {"games-act", "ch", "hb", "sv", "bk", "drv", "sys", "settings", "home"}


async def _dismiss_all(app) -> None:
    for _ in range(300):
        await asyncio.sleep(0.005)
        if len(app.screen_stack) == 1:
            if not any(w.group in GROUPS and w.is_running for w in app.workers):
                return
        elif not isinstance(app.screen, Wd.Job) and app.screen.is_mounted:
            app.screen.dismiss(None)


async def test_every_button_survives_an_empty_drive(tmp_path, monkeypatch):
    """Press every toolbar button with nothing on the drive; dismiss whatever opens."""
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: None)
    app = make_app(tmp_path)
    async with app.run_test(size=(90, 30)) as pilot:
        for key in ("drive", "games", "channels", "homebrew", "saves", "backups", "system", "settings"):
            app.action_section(key)
            await asyncio.sleep(0.02)
            for b in list(app.pane(key).query(Button)):
                if b.disabled:
                    continue
                b.press()
                await pilot.pause()
                await _dismiss_all(app)
                assert len(app.screen_stack) == 1, f"{key}.{b.id} left a dialog open"
                assert app.is_running, f"{key}.{b.id} closed the app"
            app.action_section(key)        # buttons may have switched section
    assert app.return_code in (None, 0)


async def test_every_button_survives_having_no_drive(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: None)
    app = make_app(tmp_path, drive=False)
    async with app.run_test(size=(90, 30)) as pilot:
        for key in ("drive", "games", "channels", "homebrew", "saves", "backups", "system", "settings"):
            app.action_section(key)
            await asyncio.sleep(0.02)
            for b in list(app.pane(key).query(Button)):
                if not b.disabled:
                    b.press()          # most just say "choose a drive first"
            await pilot.pause()
            await _dismiss_all(app)
            assert len(app.screen_stack) == 1 and app.is_running, key
    assert app.return_code in (None, 0)
