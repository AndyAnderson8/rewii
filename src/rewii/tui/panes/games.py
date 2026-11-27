"""Games: the PC library and the Wii drive side by side."""
from __future__ import annotations

import asyncio
from typing import Any, Optional
from collections.abc import Callable

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Input, Label, Static

from ...core import games as G
from ...core.disc import PICKABLE_EXTS, cleanrip_part
from ...core.forwarder import brick_risk
from ...core.layout import DriveLayout
from ...core.progress import UNIT_STEPS, Cancelled, Reporter
from ..base import FAILED, Pane, titles
from ..widgets import MarkTable, human_size
from ._channels import build_channels, collision_warning

GAME_EXTS = set(PICKABLE_EXTS)


class Row:
    """One line of the table: a game's library copy and/or its copy on the drive."""

    def __init__(self, key: str, lib: Optional[G.Game] = None, drive: Optional[G.Game] = None,
                 duplicate: bool = False) -> None:
        self.key = key
        self.lib = lib
        self.drive = drive
        self.duplicate = duplicate
        self.cover = ""

    @property
    def any(self) -> G.Game:
        g = self.drive or self.lib
        assert g is not None
        return g


def build_rows(lib: list[G.Game], drv: list[G.Game]) -> list[Row]:
    """Pair library and drive copies of the same game.

    Extra copies of a game (two files with the same ID on one side) get rows of
    their own, flagged ``duplicate``, so nothing is hidden and every action hits
    exactly the file shown. Files whose ID couldn't be read are never merged.
    """
    groups: dict[str, tuple[list[G.Game], list[G.Game]]] = {}
    for g in lib:
        groups.setdefault(g.key, ([], []))[0].append(g)
    for g in drv:
        groups.setdefault(g.key, ([], []))[1].append(g)
    rows: list[Row] = []
    for key, (libs, drvs) in groups.items():
        if "?" in key:
            rows += [Row(f"{key}|{g.location}|{g.path}", lib=g if g.location != "drive" else None,
                         drive=g if g.location == "drive" else None) for g in [*libs, *drvs]]
            continue
        n = max(len(libs), len(drvs))
        for i in range(n):
            rows.append(Row(key if i == 0 else f"{key}#{i}",
                            lib=libs[i] if i < len(libs) else None,
                            drive=drvs[i] if i < len(drvs) else None, duplicate=n > 1))
    rows.sort(key=lambda r: (not r.any.is_wii, r.any.title.lower(), r.any.disc_number, r.key))
    return rows


class GamesPane(Pane):
    BINDINGS = [
        ("a", "add", "Add to library"),
        ("i", "install", "Put on drive"),
        ("delete", "remove", "Remove from drive"),
        ("b", "backup", "Copy to PC"),
        ("w", "channel", "Make channel"),
        ("v", "verify", "Verify"),
        ("c", "covers", "Covers"),
        ("h", "cheats", "Cheats"),
    ]

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.rows: dict[str, Row] = {}
        self.filter = ""
        self._free = ""
        self._scanned = False

    @property
    def table(self) -> MarkTable:
        return self.query_one("#table", MarkTable)

    def compose(self) -> ComposeResult:
        yield Label("Games", classes="title")
        yield Static("Your game library on this computer and the games on the Wii's drive. "
                     "Space ticks several games; the buttons act on the ticked games you can see "
                     "(or the highlighted one). Covers and Cheats use every game on the drive "
                     "when nothing is ticked.", classes="hint")
        with Horizontal(classes="toolbar"):
            yield Button("Add games…", id="add", variant="primary")
            yield Button("Put on drive", id="install")
            yield Button("Remove from drive", id="remove")
            yield Button("Copy to PC", id="backup")
            yield Button("Make channel", id="channel")
        with Horizontal(classes="toolbar"):
            yield Button("Covers", id="covers")
            yield Button("Cheats", id="cheats")
            yield Button("Pick cheats…", id="pickcheats")
            yield Button("Rename…", id="rename")
            yield Button("Verify", id="verify")
            yield Button("Fix drive layout", id="normalize")
            yield Button("Import old WBFS drive…", id="migrate")
            yield Button("Delete from library", id="libdel")
            yield Input(placeholder="Filter…", id="filter")
        yield MarkTable(id="table")
        yield Static("", id="summary", markup=False)

    def on_mount(self) -> None:
        self.table.set_columns("Title", "ID", "Type", "Region", "PC", "Drive", "Size", "Cover", "Status")

    def button_actions(self) -> dict[str, Callable[[], Any]]:
        return {"add": self.action_add, "install": self.action_install, "remove": self.action_remove,
                "backup": self.action_backup, "channel": self.action_channel, "covers": self.action_covers,
                "cheats": self.action_cheats, "verify": self.action_verify,
                "normalize": self.action_normalize, "migrate": self.action_migrate,
                "libdel": self.action_libdel, "pickcheats": self.action_pickcheats,
                "rename": self.action_rename}

    def reload(self) -> None:
        self._scan()

    def titles_ready(self) -> None:
        """The title database finished loading: lists read before that show raw disc titles."""
        if self._scanned:
            self._scan()

    @work(thread=True, exclusive=True, group="games-scan")
    def _scan(self) -> None:
        try:
            look = self.rapp.title_lookup
            lay = self.layout_
            lib = G.scan_library(self.settings, look)
            drv = G.scan_drive(lay, look) if lay else []
            rows = build_rows(lib, drv)
            free = ""
            if lay:
                from ...core import covers
                for r in rows:
                    if r.drive:
                        try:
                            st = covers.cover_status(lay, r.drive.game_id)
                            r.cover = "yes" if any(v for k, v in st.items() if k != "cached") else "-"
                        except Exception:  # noqa: BLE001 - cosmetic
                            r.cover = "?"
                try:
                    free = f" · {human_size(G.free_space(lay.root))} free on drive"
                except OSError:
                    free = ""
        except Exception as e:  # noqa: BLE001 - an unreadable folder must not close the app
            self.scan_failed("Reading the game list", e)
            return
        self.from_thread(self._fill, rows, free)

    def _fill(self, rows: list[Row], free: str = "") -> None:
        self.rows = {r.key: r for r in rows}
        self._free = free
        self._scanned = True
        self._render_rows()

    def _status(self, r: Row) -> str:
        g = r.any
        parts = []
        if r.drive and r.drive.problem:
            parts.append(r.drive.problem)
        elif r.lib and r.lib.problem:
            parts.append(r.lib.problem)
        if r.duplicate:
            parts.append("duplicate")
        if brick_risk(g.game_id):
            parts.append("no channel")
        return " ".join(parts)

    def _render_rows(self) -> None:
        out = []
        for r in self.rows.values():
            g = r.any
            if self.filter and self.filter not in f"{g.title} {g.game_id}".lower():
                continue
            title = g.title + (f" (disc {g.disc_number + 1})" if g.disc_number else "")
            out.append((r.key, [title, g.game_id, "Wii" if g.is_wii else "GameCube", g.region,
                                "yes" if r.lib else "-", "yes" if r.drive else "-",
                                human_size(g.size), r.cover, self._status(r)]))
        self.table.fill(out)
        rows = list(self.rows.values())
        on_drive = sum(1 for r in rows if r.drive)
        dups = sum(1 for r in rows if r.duplicate)
        self.query_one("#summary", Static).update(
            f"{len(rows)} games · {on_drive} on the drive{self._free}" +
            (f" · {dups} rows are duplicate copies of the same game" if dups else "") +
            ("" if self.layout_ else " · no drive selected"))

    @on(Input.Changed, "#filter")
    def _filter(self, ev: Input.Changed) -> None:
        new = ev.value.lower().strip()
        if new == self.filter:
            return
        self.filter = new
        self.table.clear_marks()      # ticks never apply to rows you can no longer see
        self._render_rows()

    def _selected(self) -> list[Row]:
        return [self.rows[k] for k in self.table.selected_keys() if k in self.rows]

    def _done(self) -> None:
        """After a successful action: drop the ticks and re-read."""
        self.table.clear_marks()
        self.reload()

    # ------------------------------------------------------------------ actions
    @work(exclusive=True, group="games-act")
    async def action_add(self) -> None:
        paths = await self.pick("Add games to your library", exts=GAME_EXTS, multiple=True)
        if not paths:
            return
        # CleanRip dumps: picking .part0.iso is enough, the other parts are joined automatically
        paths = [p for p in paths if not (cleanrip_part(p.name) or 0) >= 1]    # only the first part
        if not paths:
            self.app.notify("Pick the .part0.iso of a CleanRip dump - the other parts are joined automatically",
                            severity="warning")
            return
        layout = self.live_layout
        put = False
        if layout is not None:
            ch = await self.choose("Also put on the drive?", f"Add {len(paths)} game(s) to the library. "
                                   "Also copy them to the Wii's drive now?",
                                   a="Library + drive", b="Library only")
            if ch is None:
                return
            put = ch == "a"
        s = self.settings
        look = self.rapp.title_lookup

        def run(progress: Any, cancel: Any) -> tuple[list[str], list[str]]:
            done, errs = [], []
            for p in paths:
                try:
                    g = G.add_to_library(s, p, scrub=s.scrub_games, lookup=look,
                                         progress=progress, cancel=cancel)
                    done.append(g.title)
                    if put and layout:
                        G.install_game(layout, g.path, title=g.title, scrub=s.scrub_games, lookup=look,
                                       progress=progress, cancel=cancel)
                except Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001 - one bad file doesn't stop the rest
                    errs.append(f"{p.name}: {self.explain(e)}")
            return done, errs

        res = await self.job(f"Adding {len(paths)} game(s)", run)
        self.reload()
        if res is not FAILED and res[1]:
            await self.message("Some games were skipped", "\n".join(res[1]))

    async def _fetch_covers(self, layout: DriveLayout, ids: list[str]) -> None:
        from ...core import covers
        s = self.settings
        res = await self.job(f"Downloading covers for {len(ids)} game(s)",
                             lambda p, c: covers.fetch_covers(layout, ids, s.cover_types, s, p, c))
        if res is not FAILED:
            self.reload()

    @work(exclusive=True, group="games-act")
    async def action_install(self) -> None:
        layout = self.need_drive()
        if layout is None:
            return
        games = [r.lib for r in self._selected() if r.lib and not r.drive]
        if not games:
            self.app.notify("Select games that are in the library but not on the drive", severity="warning")
            return
        s, look = self.settings, self.rapp.title_lookup

        def sizes() -> int:
            total = 0
            for g in games:
                try:
                    total += G.estimate_install_size(g.path, s.scrub_games)
                except Exception:  # noqa: BLE001 - the install itself reports a bad file
                    total += g.size
            return total

        need = await self.bg("Checking the games", sizes)
        if need is FAILED:
            return
        if not await self.confirm("Put games on the drive",
                                  f"Copy {len(games)} game(s) (about {human_size(need)}) to the Wii's drive?\n\n"
                                  + titles([g.title for g in games])):
            return

        def run(progress: Any, cancel: Any) -> tuple[list[str], list[str]]:
            ok, errs = [], []
            for g in games:
                try:
                    G.install_game(layout, g.path, title=g.title, scrub=s.scrub_games,
                                   lookup=look, progress=progress, cancel=cancel)
                    ok.append(g.game_id)
                except Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001 - reported per game
                    errs.append(f"{g.title}: {self.explain(e)}")
            return ok, errs

        res = await self.job("Putting games on the drive", run)
        if res is FAILED:
            self.reload()        # some may have been copied before it stopped; keep the ticks
            return
        ok, errs = res
        self._done()
        if errs:
            await self.message("Some games were skipped", "\n".join(errs))
        if ok and await self.confirm("Covers", f"Download cover art for the {len(ok)} new game(s) now?",
                                     yes="Download", no="Later"):
            await self._fetch_covers(layout, sorted(set(ok)))

    @work(exclusive=True, group="games-act")
    async def action_remove(self) -> None:
        layout = self.need_drive()
        if layout is None:
            return
        games = [r.drive for r in self._selected() if r.drive]
        if not games:
            self.app.notify("Select games that are on the drive", severity="warning")
            return
        in_lib = {r.lib.key for r in self.rows.values() if r.lib}
        body = (f"Delete {len(games)} game(s) from the Wii's drive?\n\n" +
                titles([g.title + ("" if g.key in in_lib else "   (NOT in your PC library)") for g in games]))
        if any(g.key not in in_lib for g in games):
            body += "\n\nGames that aren't in the PC library are gone for good - copy them to the PC first " \
                    "if you want to keep them."
        if not await self.confirm("Remove from drive", body, yes="Delete", danger=True):
            return

        def run(progress: Any, cancel: Any) -> list[str]:
            rep = Reporter(progress, cancel, total=len(games), unit=UNIT_STEPS)
            errs = []
            for g in games:
                rep.note(f"Removing {g.title}")
                try:
                    G.remove_game(layout, g)
                except Exception as e:  # noqa: BLE001 - reported per game
                    errs.append(f"{g.title}: {self.explain(e)}")
                rep.advance(1)
            return errs

        errs = await self.job(f"Removing {len(games)} game(s)", run, cancellable=False)
        if errs is FAILED:
            self.reload()
            return
        self._done()
        if errs:
            await self.message("Some games couldn't be removed", "\n".join(errs), error=True)

    @work(exclusive=True, group="games-act")
    async def action_backup(self) -> None:
        games = [r.drive for r in self._selected() if r.drive and not r.lib]
        if not games:
            self.app.notify("Select drive games that aren't in the library yet", severity="warning")
            return
        s, look = self.settings, self.rapp.title_lookup

        def run(progress: Any, cancel: Any) -> list[str]:
            errs = []
            for g in games:
                try:
                    G.copy_to_library(s, g, look, progress, cancel)
                except Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001 - reported per game
                    errs.append(f"{g.title}: {self.explain(e)}")
            return errs

        errs = await self.job(f"Copying {len(games)} game(s) to the PC", run)
        if errs is FAILED:
            self.reload()
            return
        self._done()
        if errs:
            await self.message("Some games weren't copied", "\n".join(errs), error=True)

    @work(exclusive=True, group="games-act")
    async def action_libdel(self) -> None:
        games = [r.lib for r in self._selected() if r.lib]
        if not games:
            self.app.notify("Select games that are in the library", severity="warning")
            return
        on_drive = {r.drive.key for r in self.rows.values() if r.drive}
        body = (f"Delete {len(games)} game file(s) from this computer?\n\n" +
                titles([g.title + ("" if g.key in on_drive else "   (NOT on the drive - this is the only copy)")
                        for g in games]) + "\n\nCopies on the drive are not touched.")
        if not await self.confirm("Delete from library", body, yes="Delete", danger=True):
            return
        s = self.settings

        def run(progress: Any, cancel: Any) -> list[str]:
            rep = Reporter(progress, cancel, total=len(games), unit=UNIT_STEPS)
            errs = []
            for g in games:
                rep.note(f"Deleting {g.title}")
                try:
                    G.delete_from_library(s, g)
                except Exception as e:  # noqa: BLE001 - reported per game
                    errs.append(f"{g.title}: {self.explain(e)}")
                rep.advance(1)
            return errs

        errs = await self.job(f"Deleting {len(games)} game(s)", run, cancellable=False)
        if errs is FAILED:
            self.reload()
            return
        self._done()
        if errs:
            await self.message("Some games couldn't be deleted", "\n".join(errs), error=True)

    @work(exclusive=True, group="games-act")
    async def action_verify(self) -> None:
        rows = self._selected()
        if not rows:
            return
        targets = [(r.any.title, r.any.path) for r in rows]

        def run(progress: Any, cancel: Any) -> list[tuple[str, G.VerifyResult]]:
            return [(t, G.verify_game(p, progress, cancel)) for t, p in targets]

        res = await self.job("Verifying", run)
        if res is not FAILED and res:
            await self.message("Verify results", "\n".join(
                f"{'OK ' if v.ok else 'BAD'}  {t}: {v.message}" for t, v in res), error=not all(v.ok for _, v in res))

    @work(exclusive=True, group="games-act")
    async def action_channel(self) -> None:
        from ...core.channels import channel_problem
        rows = self._selected()
        if not rows:
            return
        skipped, games = [], []
        for r in rows:
            g = r.any
            why = channel_problem(g.platform, g.disc_number, g.game_id, r.drive is not None)
            if why:
                skipped.append(f"{g.title}: {why}")
            else:
                games.append(g)
        if skipped:
            await self.message("Skipped", "No channel for:\n  " + "\n  ".join(skipped), error=not games)
        if not games:
            return
        s, layout, rapp = self.settings, self.live_layout, self.rapp
        n_gc = sum(1 for g in games if not g.is_wii)
        body = (f"Make {len(games)} channel(s):\n" + titles([g.title for g in games])
                + (f"\n\nWii games: launch screen '{s.forwarder_style}'." if n_gc < len(games) else "")
                + ("\nGameCube games start straight in Nintendont." if n_gc else ""))
        warn = collision_warning(games, [r.any for r in self.rows.values()])
        if warn:
            body += "\n\n" + warn
        stage = False
        if layout is not None:
            ch = await self.choose("Channels", body + "\n\nAlso put them in the drive's wad/ folder so you can "
                                   "install them with YAWMME?", a="PC + drive", b="Just save on PC")
            if ch is None:
                return
            stage = ch == "a"
        elif not await self.confirm("Channels", body + f"\n\nThey are saved in {s.channels_dir}.", yes="Make"):
            return

        res = await self.job("Making channels", lambda p, c: build_channels(
            games, s, layout, rapp.game_info, stage, p, c))
        if res is FAILED:
            return
        built, errs = res
        if errs:
            await self.message("Some channels weren't made", "\n".join(errs), error=True)
        if built:
            await self.message("Channels ready", "\n".join(c.path.name for c in built) +
                               f"\n\nSaved in {s.channels_dir}" +
                               ("\n\nOn the Wii: open YAWMME from the Homebrew Channel, pick the WAD(s) "
                                "and choose Install. See System & Guides → Install channels and WADs." if stage else "")
                               + ("\n\nGameCube channels open the game from where it is on the drive now - "
                                  "if you rename or move it, make the channel again." if n_gc else ""))

    def _drive_ids(self) -> list[str]:
        """IDs of the ticked drive games, or of every game on the drive when nothing is ticked."""
        rows = self._selected() if self.table.marked else list(self.rows.values())
        return sorted({r.drive.game_id for r in rows if r.drive})

    @work(exclusive=True, group="games-act")
    async def action_covers(self) -> None:
        layout = self.need_drive()
        if layout is None:
            return
        ids = self._drive_ids()
        if not ids:
            self.app.notify("No games on the drive to get covers for", severity="warning")
            return
        await self._fetch_covers(layout, ids)

    @work(exclusive=True, group="games-act")
    async def action_cheats(self) -> None:
        layout = self.need_drive()
        if layout is None:
            return
        ids = self._drive_ids()
        if not ids:
            self.app.notify("No games on the drive to get cheats for", severity="warning")
            return
        from ...core import cheats

        def run(progress: Any, cancel: Any) -> tuple[int, list[str], list[str]]:
            rep = Reporter(progress, cancel, total=len(ids), unit=UNIT_STEPS)
            found, none, failed = 0, [], []
            for gid in ids:
                rep.note(f"Cheats for {gid}")
                try:
                    if cheats.fetch_cheats(layout, gid):
                        found += 1
                    else:
                        none.append(gid)
                except Exception as e:  # noqa: BLE001 - offline etc.; reported below
                    failed.append(f"{gid}: {self.explain(e)}")
                rep.advance(1)
            return found, none, failed

        res = await self.job(f"Downloading cheat codes for {len(ids)} game(s)", run, quiet=True)
        if res is FAILED:
            return
        found, none, failed = res
        if failed:
            await self.message("Some cheats couldn't be downloaded", "\n".join(failed[:30]), error=True)
        self.app.notify(f"Cheats saved for {found} game(s) in txtcodes/" +
                        (f"; none exist for {len(none)}" if none else "") +
                        ". Turn them on per game in USB Loader GX.")

    @work(exclusive=True, group="games-act")
    async def action_pickcheats(self) -> None:
        layout = self.need_drive()
        if layout is None:
            return
        games = [r.drive for r in self._selected() if r.drive]
        if len(games) != 1:
            self.app.notify("Highlight one game that's on the drive", severity="warning")
            return
        g = games[0]
        from ...core import cheats
        from ..widgets import CheatPicker
        cf = await self.job(f"Getting cheats for {g.title}", lambda p, c: cheats.fetch_cheats(layout, g.game_id),
                            cancellable=False, quiet=True)
        if cf is FAILED:
            return              # the error was shown; don't claim there are no cheats
        if cf is None:
            await self.message("No cheats", f"No cheat codes are available for {g.title} [{g.game_id}].")
            return

        def current() -> list[int]:
            gct = cheats.gct_path(layout, g.game_id)
            return cheats.selected_in_gct(cf, gct.read_bytes()) if gct.is_file() else []

        pre = await self.bg("Reading the saved cheats", current)
        if pre is FAILED:
            pre = []
        res = await self.app.push_screen_wait(CheatPicker(g.title, cf.cheats, pre))
        if res is None:
            return
        sel, values = res
        saved = await self.bg("Saving cheats", lambda: cheats.build_gct(
            layout, g.game_id, [cf.cheats[i] for i in sel], values))
        if saved is FAILED:
            return
        if not sel:
            self.app.notify(f"Cheats turned off for {g.title}")
        elif g.is_wii:
            await self.message("Cheats saved", f"{len(sel)} cheat(s) saved to codes/{g.game_id}.gct.\n\n"
                               "In USB Loader GX, open the game's settings and set Ocarina to On.")
        else:
            await self.message("Cheats saved", f"{len(sel)} cheat(s) saved to codes/{g.game_id}.gct.\n\n"
                               "USB Loader GX: turn on Cheats in the game's Nintendont settings. "
                               "A GameCube channel picks them up if you make (or remake) it now.")

    @work(exclusive=True, group="games-act")
    async def action_rename(self) -> None:
        rows = self._selected()
        if len(rows) != 1:
            self.app.notify("Highlight one game", severity="warning")
            return
        r = rows[0]
        new = await self.prompt("Rename game", "New name (the [ID] is kept automatically):", r.any.title)
        if not new or not new.strip() or new.strip() == r.any.title:
            return
        name = new.strip()
        layout, s = self.layout_, self.settings

        def do() -> None:
            if r.drive and layout is not None:
                G.rename_game(layout, r.drive, name)
            if r.lib:
                G.rename_library_game(s, r.lib, name)

        res = await self.bg("Renaming", do)
        self.reload()
        if res is FAILED:
            return
        note = "The folder was renamed."
        if self.rapp.title_lookup(r.any.game_id):
            note += (" The list (and USB Loader GX) keep showing the official title for this game ID; "
                     "the new name is used for the folder on disk.")
        if r.drive and not r.any.is_wii:
            note += ("\n\nIf this game has a channel on the Wii Menu, make the channel again - "
                     "GameCube channels open the game by its folder name.")
        await self.message("Renamed", note)

    @work(exclusive=True, group="games-act")
    async def action_normalize(self) -> None:
        layout = self.need_drive()
        if layout is None:
            return
        look = self.rapp.title_lookup
        res = await self.job("Fixing drive layout",
                             lambda p, c: G.normalize_drive(layout, look, p, c))
        if res is not FAILED:
            await self.message("Drive layout", "\n".join(res) or "Everything was already in the right place.")
            self.reload()

    @work(exclusive=True, group="games-act")
    async def action_migrate(self) -> None:
        await self.message(
            "Import from an old WBFS drive",
            "Old setups formatted the whole USB drive as 'WBFS' (Windows/Mac can't open it). "
            "ReWii can copy the games out of it - from the drive itself (needs admin rights) "
            "or from a disk image of it (.img).\n\nNext, choose the image file, or type the device "
            "path into the box and press Select "
            "(e.g. /dev/sdb on Linux, /dev/disk4 on Mac, \\\\.\\PhysicalDrive2 on Windows).")
        paths = await self.pick("Old WBFS drive image or device", any_path=True)
        if not paths:
            return
        src = paths[0]
        from ...core.wbfs import WbfsPartition, find_wbfs_partition

        def probe() -> Optional[tuple[int, list[Any]]]:
            off = find_wbfs_partition(src)
            if off is None:
                return None
            part = WbfsPartition(src, off)
            try:
                return off, part.games()
            finally:
                part.close()

        try:
            found = await asyncio.to_thread(probe)
        except PermissionError:
            await self.message("Needs admin rights", "Run ReWii as administrator to read a drive directly.",
                               error=True)
            return
        except Exception as e:  # noqa: BLE001 - shown to the user
            await self.message("Couldn't read it", str(e) or type(e).__name__, error=True)
            return
        if found is None:
            await self.message("Not a WBFS drive", "No WBFS partition found there.", error=True)
            return
        off, games = found
        if not games:
            await self.message("Nothing to import", "That WBFS drive has no games on it.")
            return
        lst = titles([f"{g.title} [{g.game_id}] {human_size(g.size)}" for g in games], limit=60)
        layout: Optional[DriveLayout] = None
        if self.live_layout is not None:
            ch = await self.choose("Where to?", f"Found {len(games)} games:\n{lst}\n\nCopy them to the "
                                   "current drive too, or only into the PC library?",
                                   a="Library + drive", b="Library only")
            if ch is None:
                return
            layout = self.live_layout if ch == "a" else None
        elif not await self.confirm("Import games", f"Found {len(games)} games:\n{lst}\n\n"
                                    "Copy them into the PC library?", yes="Import"):
            return
        s, look = self.settings, self.rapp.title_lookup
        res = await self.job("Importing games",
                             lambda p, c: G.migrate_wbfs_partition(src, layout, s, None, off, look, p, c))
        self.reload()
        if res is not FAILED:
            bad = getattr(res, "problems", 0)
            await self.message("Import finished - with problems" if bad else "Import finished",
                               "\n".join(res) or "Nothing new to copy.", error=bool(bad))
