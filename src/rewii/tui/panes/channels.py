"""Channels & WADs: game channels, Virtual Console / WiiWare WADs, staging for YAWMME."""
from __future__ import annotations

import shutil
from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Label, Static

from ...core import wad as W
from ...core.forwarder import brick_risk, channel_game_id, cover_for_channel, make_channel
from ..base import Pane
from ..widgets import human_size


class ChannelsPane(Pane):
    TITLE = "Channels & WADs"
    BINDINGS = [("space", "mark", "Mark"), ("enter", "inspect", "Details")]

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.items: dict[str, tuple[str, W.WadInfo]] = {}
        self.marked: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Label("Channels & WADs", classes="title")
        yield Static("WAD files install channels on the Wii Menu: game channels made by ReWii, "
                     "Virtual Console, WiiWare… 'Put on drive' copies them to the drive's wad/ folder; "
                     "then install them on the Wii with YAWMME (see System & Guides).", classes="hint")
        with Horizontal(classes="toolbar"):
            yield Button("Import WADs…", id="import", variant="primary")
            yield Button("Channels for all drive games", id="allch")
            yield Button("Put on drive", id="stage")
            yield Button("Remove from drive", id="unstage")
            yield Button("Details", id="inspect")
            yield Button("Delete from PC", id="delete")
        yield DataTable(id="t", cursor_type="row", zebra_stripes=True)
        yield Static("", id="summary")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns(" ", "Name", "Title ID", "Type", "Region", "Where",
                                              "Size", "Check")

    def reload(self) -> None:
        self._scan()

    @work(thread=True, exclusive=True, group="ch-scan")
    def _scan(self) -> None:
        items: dict[str, tuple[str, W.WadInfo]] = {}
        for folder in (self.settings.channels_dir, self.settings.wads_dir):
            for w in W.scan_wads(folder).wads:
                items[str(w.path)] = ("PC", w)
        if self.layout_:
            for w in W.list_staged(self.layout_).wads:
                items[str(w.path)] = ("drive", w)
        self.app.call_from_thread(self._fill, items)

    def _fill(self, items) -> None:
        self.items = items
        self.marked &= set(items)
        t = self.query_one(DataTable)
        t.clear()
        risks_cnt = 0
        for key, (where, w) in sorted(items.items(), key=lambda kv: (kv[1][0], kv[1][1].name.lower())):
            risks = W.classify_risk(w, self.settings)
            flag = "BLOCKED" if W.is_blocked(risks) else ("warn" if any(r.level == "warning" for r in risks)
                                                          else "ok")
            risks_cnt += flag != "ok"
            t.add_row("✔" if key in self.marked else "", w.name or (w.path.name if w.path else "?"),
                      w.title_ascii, w.title_type, w.region,
                      "PC" if where == "PC" else "drive wad/", human_size(w.file_size), flag, key=key)
        staged = sum(1 for wh, _ in items.values() if wh == "drive")
        self.query_one("#summary", Static).update(
            f"{len(items)} WADs · {staged} ready on the drive" + (f" · {risks_cnt} need attention" if risks_cnt else ""))

    def _sel(self) -> list[str]:
        if self.marked:
            return list(self.marked)
        t = self.query_one(DataTable)
        if not t.row_count:
            return []
        return [t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value]

    def action_mark(self) -> None:
        t = self.query_one(DataTable)
        if not t.row_count:
            return
        k = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        self.marked.symmetric_difference_update({k})
        t.update_cell(k, t.ordered_columns[0].key, "✔" if k in self.marked else "")
        t.move_cursor(row=t.cursor_row + 1)

    @on(Button.Pressed)
    def _b(self, ev: Button.Pressed) -> None:
        {"import": self.action_import, "allch": self.action_allch, "stage": self.action_stage,
         "unstage": self.action_unstage, "inspect": self.action_inspect,
         "delete": self.action_delete}[ev.button.id]()

    @on(DataTable.RowSelected)
    def _row(self) -> None:
        self.action_inspect()

    @work(exclusive=True, group="ch")
    async def action_import(self) -> None:
        paths = await self.pick("Import WAD files", exts={".wad"}, multiple=True)
        if not paths:
            return
        self.settings.wads_dir.mkdir(parents=True, exist_ok=True)
        n = 0
        for p in paths:
            dst = self.settings.wads_dir / p.name
            if not dst.exists():
                shutil.copy2(p, dst)
                n += 1
        self.app.notify(f"Imported {n} WAD(s)")
        self.reload()

    @work(exclusive=True, group="ch")
    async def action_inspect(self) -> None:
        keys = self._sel()
        if not keys:
            return
        where, w = self.items[keys[0]]
        risks = W.classify_risk(w, self.settings)
        fwd = channel_game_id(w.path) if w.path else None
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
            f"File:          {w.path}",
        ]
        if fwd:
            lines.append(f"Game channel:  boots game {fwd} from USB")
        full = W.inspect_wad(w.path, verify_hashes=True) if w.path else w
        lines.append(f"Contents:      {'all OK' if full.hashes_ok else 'DAMAGED ' + str(full.bad_contents)}")
        if risks:
            lines.append("")
            lines += [f"{r.level.upper()}: {r.message}" for r in risks]
        await self.message(w.name or "WAD", "\n".join(lines), error=W.is_blocked(risks))

    @work(exclusive=True, group="ch")
    async def action_stage(self) -> None:
        if not self.need_drive():
            return
        keys = [k for k in self._sel() if self.items[k][0] == "PC"]
        if not keys:
            return
        infos = [self.items[k][1] for k in keys]
        blocked = [w for w in infos if W.is_blocked(W.classify_risk(w, self.settings))]
        if blocked:
            await self.message("Blocked", "These WADs could brick your Wii and won't be copied:\n  " +
                               "\n  ".join(f"{w.name}: " + "; ".join(r.message for r in W.classify_risk(w, self.settings) if r.blocks)
                                           for w in blocked), error=True)
            infos = [w for w in infos if w not in blocked]
        warn = [w for w in infos if any(r.level == "warning" for r in W.classify_risk(w, self.settings))]
        if warn and not await self.confirm(
                "Warnings", "\n".join(f"{w.name}: " + "; ".join(r.message for r in W.classify_risk(w, self.settings)
                                                               if r.level == "warning") for w in warn) +
                "\n\nCopy them anyway?"):
            infos = [w for w in infos if w not in warn]
        if not infos:
            return
        layout, s = self.layout_, self.settings
        await self.job("Copying WADs to the drive",
                       lambda p, c: W.stage_wads(layout, [w.path for w in infos], p, c, settings=s))
        self.marked.clear()
        self.reload()

    @work(exclusive=True, group="ch")
    async def action_unstage(self) -> None:
        if not self.need_drive():
            return
        keys = [k for k in self._sel() if self.items[k][0] == "drive"]
        if not keys:
            return
        W.unstage(self.layout_, [Path(k) for k in keys])
        self.marked.clear()
        self.reload()

    @work(exclusive=True, group="ch")
    async def action_delete(self) -> None:
        keys = [k for k in self._sel() if self.items[k][0] == "PC"]
        if not keys or not await self.confirm("Delete", f"Delete {len(keys)} WAD file(s) from this computer?",
                                              danger=True, yes="Delete"):
            return
        for k in keys:
            Path(k).unlink(missing_ok=True)
        self.marked.clear()
        self.reload()

    @work(exclusive=True, group="ch")
    async def action_allch(self) -> None:
        if not self.need_drive():
            return
        from ...core import games as G
        have = {channel_game_id(w.path) for _, w in self.items.values() if w.path}
        games = [g for g in G.scan_drive(self.layout_, self.rapp.title_lookup)
                 if g.is_wii and g.game_id not in have and not brick_risk(g.game_id)]
        if not games:
            self.app.notify("Every Wii game on the drive already has a channel")
            return
        s, layout = self.settings, self.layout_
        if not await self.confirm("Make channels", f"Make channels for {len(games)} game(s) "
                                  f"(launch screen: {s.forwarder_style}) and copy them to the drive's wad/ folder?"
                                  + ("\n\nThe Wii Menu holds about 48 channels." if len(games) > 20 else "")):
            return

        def run(progress, cancel):
            from ...core.progress import Reporter
            rep = Reporter(progress, cancel, total=len(games))
            out = []
            for g in games:
                rep.note(f"Building channel for {g.title}")
                cov = cover_for_channel(g.game_id, s.cover_lang) if s.forwarder_style == "cover" else None
                ci = make_channel(g.path, s.channels_dir, style=s.forwarder_style, cover_png=cov, title=g.title)
                W.stage_wads(layout, [ci.path], settings=s)
                out.append(ci.path.name)
                rep.advance(1)
            return out

        res = await self.job("Making channels", run)
        if res:
            await self.message("Channels ready", "\n".join(res) + "\n\nInstall them on the Wii with YAWMME "
                               "(System & Guides → Install channels and WADs).")
        self.reload()
