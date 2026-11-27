"""Homebrew: browse, install and update apps from the Open Shop Channel."""
from __future__ import annotations

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from ...core import osc
from ..base import Pane
from ..widgets import human_size


class HomebrewPane(Pane):
    TITLE = "Homebrew"
    BINDINGS = [("space", "mark", "Mark"), ("enter", "details", "Details")]

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.apps: list[osc.App] = []
        self.installed: dict[str, osc.InstalledApp] = {}     # slug/folder -> installed
        self.updates: dict[str, osc.App] = {}
        self.marked: set[str] = set()
        self.text = ""
        self.cat: str | None = None
        self.only_installed = False

    def compose(self) -> ComposeResult:
        yield Label("Homebrew", classes="title")
        yield Static("Apps from the Open Shop Channel, installed into apps/ on the drive. "
                     "They show up in the Homebrew Channel.", classes="hint")
        with Horizontal(classes="toolbar"):
            yield Button("Starter kit", id="kit", variant="primary")
            yield Button("Install / update", id="install")
            yield Button("Update all", id="updall")
            yield Button("Remove", id="remove")
            yield Button("Details", id="details")
            yield Button("Send to Wii…", id="wiiload")
        with Horizontal(classes="toolbar"):
            yield Input(placeholder="Search apps…", id="q")
            yield Select([("All categories", "")], value="", allow_blank=False, id="cat")
            yield Select([("All apps", "all"), ("Installed", "inst")], value="all", allow_blank=False, id="show")
        yield DataTable(id="t", cursor_type="row", zebra_stripes=True)
        yield Static("", id="summary")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns(" ", "App", "Category", "Latest", "Installed", "Size", "By")

    def reload(self) -> None:
        self._load(False)

    @work(thread=True, exclusive=True, group="hb-load")
    def _load(self, refresh: bool) -> None:
        err = ""
        try:
            apps = osc.fetch_index(refresh)
        except Exception as e:
            apps, err = [], str(e)
        inst = osc.installed_apps(self.layout_) if self.layout_ else []
        self.app.call_from_thread(self._set, apps, inst, err)

    def _set(self, apps, inst, err) -> None:
        self.apps = apps
        self.installed = {}
        for i in inst:
            a = osc.match_app(i, apps)
            self.installed[a.slug if a else "dir:" + i.folder] = i
        self.updates = {}
        try:
            for folder, app in osc.update_available(inst, apps).items():
                self.updates[app.slug] = app
        except Exception:
            pass
        cats = sorted({a.category for a in apps if a.category})
        sel = self.query_one("#cat", Select)
        sel.set_options([("All categories", "")] + [(c.title(), c) for c in cats])
        if err:
            self.app.notify(f"Couldn't load the app list: {err}", severity="error")
        self._fill()

    def _platform(self) -> str:
        return self.settings.console_type or ""

    def _fill(self) -> None:
        t = self.query_one(DataTable)
        t.clear()
        rows = osc.search(self.apps, self.text, self.cat or None, self._platform() or None)
        for a in rows:
            inst = self.installed.get(a.slug)
            if self.only_installed and not inst:
                continue
            ver = inst.version if inst else ""
            if a.slug in self.updates:
                ver += "  (update)"
            t.add_row("✔" if a.slug in self.marked else "", a.name, a.category, a.version, ver or "-",
                      human_size(a.file_size), a.author[:20], key=a.slug)
        if self.only_installed:
            for k, i in self.installed.items():
                if k.startswith("dir:"):
                    t.add_row("", i.display_name, "(not from OSC)", "", i.version or "?", "", i.coder, key=k)
        n_up = len(self.updates)
        self.query_one("#summary", Static).update(
            f"{len(self.apps)} apps available · {len(self.installed)} installed" +
            (f" · {n_up} update(s)" if n_up else "") + ("" if self.layout_ else " · no drive selected"))

    @on(Input.Changed, "#q")
    def _q(self, ev: Input.Changed) -> None:
        self.text = ev.value
        self._fill()

    @on(Select.Changed, "#cat")
    def _c(self, ev: Select.Changed) -> None:
        self.cat = str(ev.value) if ev.value else None
        self._fill()

    @on(Select.Changed, "#show")
    def _s(self, ev: Select.Changed) -> None:
        self.only_installed = ev.value == "inst"
        self._fill()

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

    @on(DataTable.RowSelected)
    def _row(self) -> None:
        self.action_details()

    @on(Button.Pressed)
    def _b(self, ev: Button.Pressed) -> None:
        {"kit": self.action_kit, "install": self.action_install, "updall": self.action_updall,
         "remove": self.action_remove, "details": self.action_details,
         "wiiload": self.action_wiiload}[ev.button.id]()

    def _app(self, slug: str) -> osc.App | None:
        return osc.find_app(self.apps, slug)

    @work(exclusive=True, group="hb")
    async def action_details(self) -> None:
        s = self._sel()
        if not s:
            return
        a = self._app(s[0])
        if not a:
            return
        inst = self.installed.get(a.slug)
        body = (f"{a.description_short}\n\n{a.description_long[:1500]}\n\n"
                f"Version {a.version} by {a.author} · {a.category} · {human_size(a.file_size)}\n"
                f"Works on: {', '.join(a.supported_platforms) or '?'}\n"
                f"Installed: {inst.version if inst else 'no'}")
        if a.writes_to_nand:
            body += "\n\nNote: this app changes the Wii's system memory - use with care."
        await self.message(a.name, body)

    async def _install(self, apps: list[osc.App]) -> None:
        if not self.need_drive() or not apps:
            return
        layout = self.layout_

        def run(progress, cancel):
            out = []
            for a in apps:
                out.append(osc.install_app(layout, a, progress, cancel))
            return out

        res = await self.job(f"Installing {len(apps)} app(s)", run)
        if res is not None:
            self.marked.clear()
            self._load(False)

    @work(exclusive=True, group="hb")
    async def action_install(self) -> None:
        apps = [a for a in (self._app(k) for k in self._sel()) if a]
        nand = [a.name for a in apps if a.writes_to_nand]
        if nand and not await self.confirm("Heads up", "These apps change the Wii's system memory when "
                                           "you run them:\n  " + "\n  ".join(nand) +
                                           "\n\nMake sure you have a NAND backup first. Install anyway?"):
            return
        await self._install(apps)

    @work(exclusive=True, group="hb")
    async def action_updall(self) -> None:
        if not self.updates:
            self.app.notify("Everything is up to date")
            return
        if await self.confirm("Update all", "Update:\n  " + "\n  ".join(a.name for a in self.updates.values())):
            await self._install(list(self.updates.values()))

    @work(exclusive=True, group="hb")
    async def action_remove(self) -> None:
        if not self.need_drive():
            return
        targets = [(k, self.installed[k]) for k in self._sel() if k in self.installed]
        if not targets:
            self.app.notify("Select installed apps", severity="warning")
            return
        if not await self.confirm("Remove apps", "Remove from the drive:\n  " +
                                  "\n  ".join(i.display_name for _, i in targets), danger=True, yes="Remove"):
            return
        for _, i in targets:
            osc.remove_app(self.layout_, i.folder)
        self.marked.clear()
        self._load(False)

    @work(exclusive=True, group="hb")
    async def action_kit(self) -> None:
        if not self.need_drive():
            return
        plat = self._platform()
        ess = osc.starter_kit(plat, "essential")
        rec = [x for x in osc.starter_kit(plat, "all") if x not in ess]
        body = ("Essential:\n" + "\n".join(f"  {x.slug} – {x.why}" for x in ess) +
                "\n\nRecommended extras:\n" + "\n".join(f"  {x.slug} – {x.why}" for x in rec))
        if not plat:
            body += "\n\n(Tip: set your console type in Settings for the right cIOS installer.)"
        choice = await self.confirm("Starter kit", body, yes="Essential + extras", no="Essential only")
        layout = self.layout_
        which = "all" if choice else "essential"
        res = await self.job("Installing the starter kit",
                             lambda p, c: osc.install_starter_kit(layout, plat, which, p, c, apps=self.apps or None))
        if res is not None:
            lines = [f"Installed: {', '.join(res.installed) or 'nothing new'}"]
            if res.unsupported:
                lines.append(f"Not for this console: {', '.join(res.unsupported)}")
            if res.not_found:
                lines.append(f"Not found on the shop: {', '.join(res.not_found)}")
            if res.failed:
                lines.append("Failed: " + ", ".join(f"{k} ({v})" for k, v in dict(res.failed).items()))
            await self.message("Starter kit", "\n".join(lines))
            self._load(False)

    @work(exclusive=True, group="hb")
    async def action_wiiload(self) -> None:
        sel = self._sel()
        a = self._app(sel[0]) if sel else None
        if not a:
            return
        ip = await self.prompt("Send to Wii", "Open the Homebrew Channel on the Wii, press HOME to see its "
                               "IP address, and enter it here:", getattr(self.settings, "wii_ip", ""))
        if not ip:
            return
        from ...core import wiiload

        def run(progress, cancel):
            z = osc.download_archive(a, progress, cancel)
            wiiload.send(ip.strip(), z, progress=progress, cancel=cancel)

        await self.job(f"Sending {a.name} to the Wii", run)
