"""Channels & WADs: game channels, Virtual Console / WiiWare WADs, staging for YAWMME."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Optional
from collections.abc import Callable

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Label, Static

from ...core import wad as W
from ...core.forwarder import channel_game_id
from ...core.progress import Reporter
from ..base import FAILED, Pane, titles
from ..widgets import MarkTable, human_size
from ._channels import build_channels, collision_warning


class Item:
    """A WAD on the PC or in the drive's wad/ folder."""

    def __init__(self, where: str, info: W.WadInfo, path: Path, game_id: Optional[str]) -> None:
        self.where = where              # "PC" | "drive"
        self.info = info
        self.path = path
        self.game_id = game_id          # game a ReWii channel boots, if it is one

    @property
    def name(self) -> str:
        return self.info.name or self.path.name


class ChannelsPane(Pane):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.items: dict[str, Item] = {}

    @property
    def table(self) -> MarkTable:
        return self.query_one("#t", MarkTable)

    def compose(self) -> ComposeResult:
        yield Label("Channels & WADs", classes="title")
        yield Static("WAD files install channels on the Wii Menu: game channels made by ReWii, "
                     "Virtual Console, WiiWare… 'Put on drive' copies them to the drive's wad/ folder; "
                     "then install them on the Wii with YAWMME (see System & Guides). "
                     "Space ticks several; Enter shows details.", classes="hint")
        with Horizontal(classes="toolbar"):
            yield Button("Import WADs…", id="import", variant="primary")
            yield Button("Channels for all drive games", id="allch")
            yield Button("Put on drive", id="stage")
            yield Button("Remove from drive", id="unstage")
            yield Button("Details", id="inspect")
            yield Button("Delete from PC", id="delete")
        yield MarkTable(id="t")
        yield Static("", id="summary", markup=False)

    def on_mount(self) -> None:
        self.table.set_columns("Name", "Title ID", "Type", "Region", "Where", "Size", "Check")

    def button_actions(self) -> dict[str, Callable[[], Any]]:
        return {"import": self.action_import, "allch": self.action_allch, "stage": self.action_stage,
                "unstage": self.action_unstage, "inspect": self.action_inspect,
                "delete": self.action_delete}

    def reload(self) -> None:
        self._scan()

    @work(thread=True, exclusive=True, group="ch-scan")
    def _scan(self) -> None:
        try:
            items: dict[str, Item] = {}
            found: list[tuple[str, W.WadInfo]] = []
            for folder in (self.settings.channels_dir, self.settings.wads_dir):
                found += [("PC", w) for w in W.scan_wads(folder).wads]
            lay = self.layout_
            if lay:
                found += [("drive", w) for w in W.list_staged(lay).wads]
            for where, w in found:
                if w.path is None:
                    continue
                try:
                    gid = channel_game_id(w.path)
                except Exception:  # noqa: BLE001 - not a ReWii channel
                    gid = None
                items[str(w.path)] = Item(where, w, w.path, gid)
        except Exception as e:  # noqa: BLE001 - an unreadable folder must not close the app
            self.scan_failed("Reading the WAD list", e)
            return
        self.from_thread(self._fill, items)

    def _fill(self, items: dict[str, Item]) -> None:
        self.items = items
        rows = []
        risks_cnt = 0
        for key, it in sorted(items.items(), key=lambda kv: (kv[1].where, kv[1].name.lower(), kv[0])):
            w = it.info
            risks = W.classify_risk(w, self.settings)
            flag = "BLOCKED" if W.is_blocked(risks) else ("warn" if any(r.level == "warning" for r in risks)
                                                          else "ok")
            risks_cnt += flag != "ok"
            rows.append((key, [it.name, w.title_ascii, w.title_type, w.region,
                               "PC" if it.where == "PC" else "drive wad/", human_size(w.file_size), flag]))
        self.table.fill(rows)
        staged = sum(1 for it in items.values() if it.where == "drive")
        self.query_one("#summary", Static).update(
            f"{len(items)} WADs · {staged} ready on the drive" + (f" · {risks_cnt} need attention" if risks_cnt else ""))

    def _sel(self, where: Optional[str] = None) -> list[Item]:
        return [self.items[k] for k in self.table.selected_keys()
                if k in self.items and (where is None or self.items[k].where == where)]

    def _done(self) -> None:
        self.table.clear_marks()
        self.reload()

    @on(DataTable.RowSelected)
    def _row(self) -> None:
        self.action_inspect()

    @work(exclusive=True, group="ch")
    async def action_import(self) -> None:
        paths = await self.pick("Import WAD files", exts={".wad"}, multiple=True)
        if not paths:
            return
        dest = self.settings.wads_dir

        def run(progress: Any, cancel: Any) -> tuple[int, list[str]]:
            rep = Reporter(progress, cancel, total=len(paths))
            dest.mkdir(parents=True, exist_ok=True)
            n, skipped = 0, []
            for p in paths:
                rep.note(f"Copying {p.name}")
                dst = dest / p.name
                if dst.exists():
                    skipped.append(f"{p.name}: a WAD with this name is already in the library")
                else:
                    tmp = dst.with_name(dst.name + ".part")
                    try:
                        shutil.copy2(p, tmp)
                        tmp.replace(dst)
                    finally:
                        tmp.unlink(missing_ok=True)
                    n += 1
                rep.advance(1)
            return n, skipped

        res = await self.job(f"Importing {len(paths)} WAD(s)", run, quiet=True)
        self.reload()
        if res is FAILED:
            return
        self.app.notify(f"Imported {res[0]} WAD(s)")
        if res[1]:
            await self.message("Some WADs were skipped", "\n".join(res[1]))

    @work(exclusive=True, group="ch")
    async def action_inspect(self) -> None:
        cur = self.table.current_key()
        it = self.items.get(cur) if cur else None      # Details is about the highlighted row
        if it is None:
            return
        w = it.info
        full = await self.job(f"Checking {it.name}", lambda p, c: W.inspect_wad(it.path, verify_hashes=True),
                              cancellable=False, quiet=True)
        if full is FAILED:
            return
        risks = W.classify_risk(w, self.settings)
        lines = [
            f"Name:          {w.name}",
            f"Title ID:      {w.pretty_id}  ({w.title_ascii})",
            f"Type:          {w.title_type}",
            f"Region:        {w.region}",
            f"Version:       {w.version}",
            f"Runs on IOS:   {w.required_ios or '-'}",
            f"Platform:      {'Wii U (vWii)' if w.platform == 'vwii' else 'Wii'}",
            f"Signature:     {w.signature_status}",
            f"Size:          {human_size(w.file_size)} (uses {human_size(w.installed_size)} of Wii memory)",
            f"File:          {it.path}",
        ]
        if it.game_id:
            lines.append(f"Game channel:  boots game {it.game_id} from USB")
        lines.append(f"Contents:      {'all OK' if full.hashes_ok else 'DAMAGED ' + str(full.bad_contents)}")
        if risks:
            lines.append("")
            lines += [f"{r.level.upper()}: {r.message}" for r in risks]
        await self.message(w.name or "WAD", "\n".join(lines), error=W.is_blocked(risks))

    @work(exclusive=True, group="ch")
    async def action_stage(self) -> None:
        layout = self.need_drive()
        if layout is None:
            return
        its = self._sel("PC")
        if not its:
            self.app.notify("Select WADs that are on the PC", severity="warning")
            return
        s = self.settings
        risk = {str(it.path): W.classify_risk(it.info, s) for it in its}
        blocked = [it for it in its if W.is_blocked(risk[str(it.path)])]
        if blocked:
            await self.message("Blocked", "These WADs could brick your Wii and won't be copied:\n  " +
                               "\n  ".join(f"{it.name}: " + "; ".join(r.message for r in risk[str(it.path)] if r.blocks)
                                           for it in blocked), error=True)
            its = [it for it in its if it not in blocked]
        warn = [it for it in its if any(r.level == "warning" for r in risk[str(it.path)])]
        if warn:
            ch = await self.choose(
                "Warnings", "\n".join(f"{it.name}: " + "; ".join(r.message for r in risk[str(it.path)]
                                                                 if r.level == "warning") for it in warn) +
                "\n\nCopy these too?", a="Copy them too", b="Skip these")
            if ch is None:
                return
            if ch == "b":
                its = [it for it in its if it not in warn]
        if not its:
            return
        paths = [it.path for it in its]
        res = await self.job("Copying WADs to the drive",
                             lambda p, c: W.stage_wads(layout, paths, p, c, settings=s))
        if res is FAILED:
            self.reload()
            return
        self._done()

    @work(exclusive=True, group="ch")
    async def action_unstage(self) -> None:
        layout = self.need_drive()
        if layout is None:
            return
        its = self._sel("drive")
        if not its:
            self.app.notify("Select WADs that are in the drive's wad/ folder", severity="warning")
            return
        on_pc = {(it.info.title_id, it.info.version, it.info.file_size)
                 for it in self.items.values() if it.where == "PC"}
        only = [it for it in its if (it.info.title_id, it.info.version, it.info.file_size) not in on_pc]
        body = (f"Delete {len(its)} WAD file(s) from the drive's wad/ folder?\n\n" +
                titles([it.name + ("   (no copy on this computer)" if it in only else "") for it in its]) +
                "\n\nChannels already installed on the Wii are not affected.")
        if not await self.confirm("Remove from drive", body, danger=True, yes="Delete"):
            return
        paths = [it.path for it in its]
        res = await self.job("Removing WADs from the drive", lambda p, c: W.unstage(layout, paths),
                             cancellable=False, quiet=True)
        if res is FAILED:
            self.reload()
            return
        self.app.notify(f"Removed {len(res)} WAD(s) from the drive")
        self._done()

    @work(exclusive=True, group="ch")
    async def action_delete(self) -> None:
        its = self._sel("PC")
        if not its:
            self.app.notify("Select WADs that are on the PC", severity="warning")
            return
        if not await self.confirm("Delete", f"Delete {len(its)} WAD file(s) from this computer?\n\n" +
                                  titles([it.name for it in its]), danger=True, yes="Delete"):
            return
        paths = [it.path for it in its]

        def run(progress: Any, cancel: Any) -> list[str]:
            errs = []
            for p in paths:
                try:
                    p.unlink(missing_ok=True)
                except OSError as e:
                    errs.append(f"{p.name}: {self.explain(e)}")
            return errs

        errs = await self.job("Deleting WADs", run, cancellable=False, quiet=True)
        if errs is FAILED:
            self.reload()
            return
        self._done()
        if errs:
            await self.message("Some WADs couldn't be deleted", "\n".join(errs), error=True)

    @work(exclusive=True, group="ch")
    async def action_allch(self) -> None:
        layout = self.need_drive()
        if layout is None:
            return
        from ...core import games as G
        from ...core.channels import channel_problem
        have = {it.game_id for it in self.items.values() if it.game_id}
        look = self.rapp.title_lookup
        on_drive = await self.bg("Reading the games on the drive", lambda: G.scan_drive(layout, look))
        if on_drive is FAILED:
            return
        games = [g for g in on_drive
                 if g.game_id not in have and not channel_problem(g.platform, g.disc_number, g.game_id, True)]
        if not games:
            self.app.notify("Every game on the drive that can have a channel already has one"
                            if on_drive else "There are no games on the drive yet")
            return
        s, rapp = self.settings, self.rapp
        body = (f"Make channels for {len(games)} game(s) (launch screen: {s.forwarder_style}) and copy them "
                "to the drive's wad/ folder?\n\n" + titles([g.title for g in games])
                + ("\n\nThe Wii Menu holds about 48 channels." if len(games) > 20 else ""))
        warn = collision_warning(games, on_drive)
        if warn:
            body += "\n\n" + warn
        if not await self.confirm("Make channels", body, yes="Make channels"):
            return
        res = await self.job("Making channels", lambda p, c: build_channels(
            games, s, layout, rapp.game_info, True, p, c))
        self.reload()
        if res is FAILED:
            return
        built, errs = res
        if errs:
            await self.message("Some channels weren't made", "\n".join(errs), error=True)
        if built:
            await self.message("Channels ready", "\n".join(c.path.name for c in built) +
                               "\n\nInstall them on the Wii with YAWMME "
                               "(System & Guides → Install channels and WADs).")
