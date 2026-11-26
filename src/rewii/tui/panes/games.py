"""Games: the PC library and the Wii drive side by side."""
from __future__ import annotations

from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Input, Label, Static

from ...core import games as G
from ...core.forwarder import brick_risk
from ..base import Pane
from ..widgets import human_size

GAME_EXTS = {".wbfs", ".iso", ".ciso", ".gcm", ".rvz", ".wia", ".gcz", ".nkit", ".wdf"}


class Row:
    def __init__(self, key: str):
        self.key = key
        self.lib: G.Game | None = None
        self.drive: G.Game | None = None

    @property
    def any(self) -> G.Game:
        return self.drive or self.lib  # type: ignore[return-value]


class GamesPane(Pane):
    TITLE = "Games"
    BINDINGS = [
        ("space", "mark", "Mark"),
        ("a", "add", "Add to library"),
        ("i", "install", "Put on drive"),
        ("delete", "remove", "Remove from drive"),
        ("b", "backup", "Copy to PC"),
        ("w", "channel", "Make channel"),
        ("v", "verify", "Verify"),
        ("c", "covers", "Covers"),
        ("h", "cheats", "Cheats"),
    ]

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.rows: dict[str, Row] = {}
        self.marked: set[str] = set()
        self.filter = ""

    def compose(self) -> ComposeResult:
        yield Label("Games", classes="title")
        yield Static("Your game library on this computer and the games on the Wii's drive. "
                     "Space marks several games; the buttons act on marked games (or the highlighted one).",
                     classes="hint")
        with Horizontal(classes="toolbar"):
            yield Button("Add games…", id="add", variant="primary")
            yield Button("Put on drive", id="install")
            yield Button("Remove from drive", id="remove")
            yield Button("Copy to PC", id="backup")
            yield Button("Make channel", id="channel")
        with Horizontal(classes="toolbar"):
            yield Button("Covers", id="covers")
            yield Button("Cheats", id="cheats")
            yield Button("Verify", id="verify")
            yield Button("Fix drive layout", id="normalize")
            yield Button("Import old WBFS drive…", id="migrate")
            yield Button("Delete from library", id="libdel")
            yield Input(placeholder="Filter…", id="filter")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="summary")

    def on_mount(self) -> None:
        t = self.query_one(DataTable)
        t.add_columns(" ", "Title", "ID", "Type", "Region", "PC", "Drive", "Size", "Cover", "Status")

    def reload(self) -> None:
        self._scan()

    @work(thread=True, exclusive=True, group="games-scan")
    def _scan(self) -> None:
        look = self.rapp.title_lookup
        lib = G.scan_library(self.settings, look)
        drv = G.scan_drive(self.layout_, look) if self.layout_ else []
        rows: dict[str, Row] = {}
        for g in lib:
            rows.setdefault(g.key, Row(g.key)).lib = g
        for g in drv:
            rows.setdefault(g.key, Row(g.key)).drive = g
        self.app.call_from_thread(self._fill, rows)

    def _fill(self, rows: dict[str, Row]) -> None:
        self.rows = rows
        self.marked &= set(rows)
        t = self.query_one(DataTable)
        cur = t.cursor_row
        t.clear()
        from ...core import covers
        ordered = sorted(rows.values(), key=lambda r: (not r.any.is_wii, r.any.title.lower(), r.any.disc_number))
        for r in ordered:
            g = r.any
            if self.filter and self.filter not in f"{g.title} {g.game_id}".lower():
                continue
            title = g.title + (f" (disc {g.disc_number + 1})" if g.disc_number else "")
            cov = ""
            if r.drive and self.layout_:
                try:
                    st = covers.cover_status(self.layout_, g.game_id)
                    cov = "yes" if any(v for k, v in st.items() if k != "cached") else "-"
                except Exception:
                    cov = "?"
            status = (r.drive.problem if r.drive and r.drive.problem else
                      r.lib.problem if r.lib and r.lib.problem else "")
            if brick_risk(g.game_id):
                status = (status + " no channel").strip()
            t.add_row("✔" if r.key in self.marked else "", title, g.game_id,
                      "Wii" if g.is_wii else "GameCube", g.region,
                      "yes" if r.lib else "-", "yes" if r.drive else "-",
                      human_size(g.size), cov, status, key=r.key)
        if cur is not None and t.row_count:
            t.move_cursor(row=min(cur, t.row_count - 1))
        on_drive = sum(1 for r in rows.values() if r.drive)
        free = ""
        if self.layout_:
            try:
                free = f" · {human_size(G.free_space(self.layout_.root))} free on drive"
            except OSError:
                free = ""
        self.query_one("#summary", Static).update(
            f"{len(rows)} games · {on_drive} on the drive{free}" +
            ("" if self.layout_ else " · no drive selected"))

    @on(Input.Changed, "#filter")
    def _filter(self, ev: Input.Changed) -> None:
        self.filter = ev.value.lower().strip()
        self._fill(self.rows)

    def _selected(self) -> list[Row]:
        if self.marked:
            return [self.rows[k] for k in self.marked if k in self.rows]
        t = self.query_one(DataTable)
        if not t.row_count:
            return []
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        return [self.rows[key]] if key in self.rows else []

    def action_mark(self) -> None:
        t = self.query_one(DataTable)
        if not t.row_count:
            return
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        self.marked.symmetric_difference_update({key})
        t.update_cell(key, t.ordered_columns[0].key, "✔" if key in self.marked else "")
        t.move_cursor(row=t.cursor_row + 1)

    @on(Button.Pressed)
    def _btn(self, ev: Button.Pressed) -> None:
        act = {"add": self.action_add, "install": self.action_install, "remove": self.action_remove,
               "backup": self.action_backup, "channel": self.action_channel, "covers": self.action_covers,
               "cheats": self.action_cheats, "verify": self.action_verify,
               "normalize": self.action_normalize, "migrate": self.action_migrate,
               "libdel": self.action_libdel}.get(ev.button.id or "")
        if act:
            act()

    # ------------------------------------------------------------------ actions
    @work(exclusive=True, group="games-act")
    async def action_add(self) -> None:
        paths = await self.pick("Add games to your library", exts=GAME_EXTS, multiple=True,
                                start=Path(self.settings.last_drive or Path.home()))
        if not paths:
            return
        import re
        # CleanRip dumps: picking .part0.iso is enough, the other parts are joined automatically
        paths = [p for p in paths if not re.search(r"\.part[1-9]\d*\.iso$", p.name, re.I)]
        move = False
        put = self.layout_ is not None and await self.confirm(
            "Also put on the drive?", f"Add {len(paths)} game(s) to the library. "
            "Also copy them to the Wii's drive now?", yes="Yes, both", no="Library only")
        s = self.settings
        look = self.rapp.title_lookup
        layout = self.layout_

        def run(progress, cancel):
            done, errs = [], []
            for i, p in enumerate(paths, 1):
                try:
                    g = G.add_to_library(s, p, move=move, scrub=s.scrub_games, lookup=look,
                                         progress=progress, cancel=cancel)
                    done.append(g.title)
                    if put and layout:
                        G.install_game(layout, g.path, title=g.title, scrub=s.scrub_games, lookup=look,
                                       progress=progress, cancel=cancel)
                except G.GameError as e:
                    errs.append(f"{p.name}: {e}")
            return done, errs

        res = await self.job(f"Adding {len(paths)} game(s)", run)
        self.reload()
        if res and res[1]:
            await self.message("Some games were skipped", "\n".join(res[1]))

    @work(exclusive=True, group="games-act")
    async def action_install(self) -> None:
        if not self.need_drive():
            return
        rows = [r for r in self._selected() if r.lib and not r.drive]
        if not rows:
            self.app.notify("Select games that are in the library but not on the drive", severity="warning")
            return
        need = sum(G.estimate_install_size(r.lib.path, self.settings.scrub_games) for r in rows
                   if not r.lib.needs_conversion)
        if not await self.confirm("Put games on the drive",
                                  f"Copy {len(rows)} game(s) (~{human_size(need)}) to the Wii's drive?"):
            return
        layout, s, look = self.layout_, self.settings, self.rapp.title_lookup

        def run(progress, cancel):
            errs = []
            for r in rows:
                try:
                    G.install_game(layout, r.lib.path, title=r.lib.title, scrub=s.scrub_games,
                                   lookup=look, progress=progress, cancel=cancel)
                except G.GameError as e:
                    errs.append(f"{r.lib.title}: {e}")
            return errs

        errs = await self.job("Putting games on the drive", run)
        self.marked.clear()
        self.reload()
        if errs:
            await self.message("Some games were skipped", "\n".join(errs))
        elif errs is not None and await self.confirm(
                "Covers", "Download cover art for the new games now?", yes="Download", no="Later"):
            self.action_covers()

    @work(exclusive=True, group="games-act")
    async def action_remove(self) -> None:
        if not self.need_drive():
            return
        rows = [r for r in self._selected() if r.drive]
        if not rows:
            return
        unsaved = [r.drive.title for r in rows if not r.lib]
        body = f"Delete {len(rows)} game(s) from the Wii's drive?"
        if unsaved:
            body += ("\n\nNot in your PC library (copy them to the PC first if you want to keep them):\n  "
                     + "\n  ".join(unsaved))
        if not await self.confirm("Remove from drive", body, yes="Delete", danger=True):
            return
        for r in rows:
            G.remove_game(self.layout_, r.drive)
        self.marked.clear()
        self.app.notify(f"Removed {len(rows)} game(s)")
        self.reload()

    @work(exclusive=True, group="games-act")
    async def action_backup(self) -> None:
        rows = [r for r in self._selected() if r.drive and not r.lib]
        if not rows:
            self.app.notify("Select drive games that aren't in the library yet", severity="warning")
            return
        s, look = self.settings, self.rapp.title_lookup

        def run(progress, cancel):
            for r in rows:
                G.copy_to_library(s, r.drive, look, progress, cancel)

        await self.job(f"Copying {len(rows)} game(s) to the PC", run)
        self.reload()

    @work(exclusive=True, group="games-act")
    async def action_libdel(self) -> None:
        rows = [r for r in self._selected() if r.lib]
        if not rows:
            return
        if not await self.confirm("Delete from library",
                                  f"Delete {len(rows)} game file(s) from this computer? "
                                  "Copies on the drive are not touched.", yes="Delete", danger=True):
            return
        for r in rows:
            G.delete_from_library(self.settings, r.lib)
        self.marked.clear()
        self.reload()

    @work(exclusive=True, group="games-act")
    async def action_verify(self) -> None:
        rows = self._selected()
        if not rows:
            return
        targets = [(r.any.title, (r.drive or r.lib).path) for r in rows]

        def run(progress, cancel):
            return [(t, G.verify_game(p, progress, cancel)) for t, p in targets]

        res = await self.job("Verifying", run)
        if res:
            await self.message("Verify results", "\n".join(
                f"{'OK ' if v.ok else 'BAD'}  {t}: {v.message}" for t, v in res), error=not all(v.ok for _, v in res))

    @work(exclusive=True, group="games-act")
    async def action_channel(self) -> None:
        from ...core.channels import build_channel, channel_problem
        rows = self._selected()
        if not rows:
            return
        bad = [(r.any.title, channel_problem(r.any.platform, r.any.disc_number, r.any.game_id, r.drive is not None))
               for r in rows]
        skipped = [f"{t}: {p}" for t, p in bad if p]
        rows = [r for r, (_t, p) in zip(rows, bad) if not p]
        if skipped:
            await self.message("Skipped", "No channel for:\n  " + "\n  ".join(skipped), error=not rows)
        if not rows:
            return
        s, layout, rapp = self.settings, self.layout_, self.rapp
        n_gc = sum(1 for r in rows if not r.any.is_wii)
        stage = layout is not None and await self.confirm(
            "Channels", f"Make {len(rows)} channel(s)."
            + (f"\nWii games: launch screen '{s.forwarder_style}'." if n_gc < len(rows) else "")
            + (f"\nGameCube games start straight in Nintendont." if n_gc else "")
            + "\n\nAlso put them in the drive's wad/ folder so you can install them with YAWMME?",
            yes="Yes", no="Just save on PC")

        def run(progress, cancel):
            from ...core.progress import Reporter
            rep = Reporter(progress, cancel, total=len(rows))
            out = []
            for r in rows:
                g = r.drive if (r.drive and not r.any.is_wii) else (r.drive or r.lib)
                rep.note(f"Building channel for {g.title}")
                ci = build_channel(g.path, s, drive_root=layout.root if layout else None, title=g.title,
                                   info=rapp.game_info(g.game_id))
                if stage:
                    layout.wad.mkdir(parents=True, exist_ok=True)
                    (layout.wad / ci.path.name).write_bytes(ci.path.read_bytes())
                out.append(ci)
                rep.advance(1)
            return out

        res = await self.job("Making channels", run)
        if res:
            await self.message("Channels ready", "\n".join(c.path.name for c in res) +
                               f"\n\nSaved in {s.channels_dir}" +
                               ("\n\nOn the Wii: open YAWMME from the Homebrew Channel, pick the WAD(s) "
                                "and choose Install. See System & Guides → Install channels and WADs." if stage else "")
                               + ("\n\nGameCube channels open the game from where it is on the drive now - "
                                  "if you rename or move it, make the channel again." if n_gc else ""))

    @work(exclusive=True, group="games-act")
    async def action_covers(self) -> None:
        if not self.need_drive():
            return
        ids = sorted({r.drive.game_id for r in self.rows.values() if r.drive})
        if not ids:
            return
        from ...core import covers
        layout, s = self.layout_, self.settings

        def run(progress, cancel):
            return covers.fetch_covers(layout, ids, s.cover_types, s, progress, cancel)

        res = await self.job(f"Downloading covers for {len(ids)} game(s)", run)
        if res is not None:
            self.reload()

    @work(exclusive=True, group="games-act")
    async def action_cheats(self) -> None:
        if not self.need_drive():
            return
        ids = sorted({r.drive.game_id for r in (self._selected() if self.marked else self.rows.values())
                      if r.drive})
        if not ids:
            return
        from ...core import cheats
        layout, s = self.layout_, self.settings

        def run(progress, cancel):
            from ...core.progress import Reporter
            rep = Reporter(progress, cancel, total=len(ids))
            found, none = 0, []
            for gid in ids:
                rep.note(f"Cheats for {gid}")
                if cheats.fetch_cheats(layout, gid):
                    found += 1
                else:
                    none.append(gid)
                rep.advance(1)
            return found, none

        res = await self.job(f"Downloading cheat codes for {len(ids)} game(s)", run)
        if res is not None:
            self.app.notify(f"Cheats saved for {res[0]} game(s) in txtcodes/" +
                            (f"; none exist for {len(res[1])}" if res[1] else "") +
                            ". Turn them on per game in USB Loader GX.")

    @work(exclusive=True, group="games-act")
    async def action_normalize(self) -> None:
        if not self.need_drive():
            return
        layout, look = self.layout_, self.rapp.title_lookup
        res = await self.job("Fixing drive layout",
                             lambda p, c: G.normalize_drive(layout, look, p, c))
        if res is not None:
            await self.message("Drive layout", "\n".join(res) or "Everything was already in the right place.")
            self.reload()

    @work(exclusive=True, group="games-act")
    async def action_migrate(self) -> None:
        await self.message(
            "Import from an old WBFS drive",
            "Old setups formatted the whole USB drive as 'WBFS' (Windows/Mac can't open it). "
            "ReWii can copy the games out of it - from the drive itself (needs admin rights) "
            "or from a disk image of it (.img).\n\nNext, choose the image file or device "
            "(e.g. /dev/sdb on Linux, /dev/disk4 on Mac, \\\\.\\PhysicalDrive2 on Windows).")
        paths = await self.pick("Old WBFS drive image or device", exts={".img", ".bin", ".wbfs", ".raw"})
        if not paths:
            return
        src = paths[0]
        from ...core.wbfs import WbfsPartition, find_wbfs_partition
        try:
            off = find_wbfs_partition(src)
            if off is None:
                await self.message("Not a WBFS drive", "No WBFS partition found there.", error=True)
                return
            part = WbfsPartition(src, off)
            games = part.games()
            part.close()
        except PermissionError:
            await self.message("Needs admin rights", "Run ReWii as administrator to read a drive directly.",
                               error=True)
            return
        except Exception as e:
            await self.message("Couldn't read it", str(e), error=True)
            return
        lst = "\n".join(f"  {g.title} [{g.game_id}] {human_size(g.size)}" for g in games)
        to_drive = self.layout_ is not None and await self.confirm(
            "Where to?", f"Found {len(games)} games:\n{lst}\n\nCopy them to the current drive too, "
            "or only into the PC library?", yes="Library + drive", no="Library only")
        s, layout, look = self.settings, self.layout_ if to_drive else None, self.rapp.title_lookup
        res = await self.job("Importing games",
                             lambda p, c: G.migrate_wbfs_partition(src, layout, s, None, off, look, p, c))
        if res is not None:
            await self.message("Import finished", "\n".join(res) or "Nothing new to copy.")
            self.reload()
