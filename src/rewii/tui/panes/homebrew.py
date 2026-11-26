"""Homebrew: browse, install and update apps from the Open Shop Channel."""
from __future__ import annotations

from typing import Any, Optional
from collections.abc import Callable

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from ...core import osc
from ...core.progress import Cancelled, Reporter
from ..base import FAILED, Pane, kit_lines, titles
from ..widgets import MarkTable, human_size


class HomebrewPane(Pane):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.apps: list[osc.App] = []
        self.installed: dict[str, osc.InstalledApp] = {}     # slug or "dir:<folder>" -> installed
        self.updates: dict[str, osc.App] = {}
        self.text = ""
        self.cat: Optional[str] = None
        self.only_installed = False
        self._cats: list[str] = []

    @property
    def table(self) -> MarkTable:
        return self.query_one("#t", MarkTable)

    def compose(self) -> ComposeResult:
        yield Label("Homebrew", classes="title")
        yield Static("Apps from the Open Shop Channel, installed into apps/ on the drive. "
                     "They show up in the Homebrew Channel. Space ticks several; Enter shows details.",
                     classes="hint")
        with Horizontal(classes="toolbar"):
            yield Button("Starter kit", id="kit", variant="primary")
            yield Button("Install / update", id="install")
            yield Button("Update all", id="updall")
            yield Button("Remove", id="remove")
            yield Button("Details", id="details")
            yield Button("Send to Wii…", id="wiiload")
            yield Button("Refresh list", id="refresh")
        with Horizontal(classes="toolbar"):
            yield Input(placeholder="Search apps…", id="q")
            yield Select([("All categories", "")], value="", allow_blank=False, id="cat")
            yield Select([("All apps", "all"), ("Installed", "inst")], value="all", allow_blank=False, id="show")
        yield MarkTable(id="t")
        yield Static("", id="summary", markup=False)

    def on_mount(self) -> None:
        self.table.set_columns("App", "Category", "Latest", "Installed", "Size", "By")

    def button_actions(self) -> dict[str, Callable[[], Any]]:
        return {"kit": self.action_kit, "install": self.action_install, "updall": self.action_updall,
                "remove": self.action_remove, "details": self.action_details,
                "wiiload": self.action_wiiload, "refresh": self.refresh_data}

    def reload(self) -> None:
        self._load(False)

    def refresh_data(self) -> None:
        """Download the app list again instead of using the copy cached for a day."""
        self.app.notify("Downloading the app list…")
        self._load(True)

    @work(thread=True, exclusive=True, group="hb-load")
    def _load(self, refresh: bool) -> None:
        err = ""
        try:
            try:
                apps = osc.fetch_index(refresh)
            except Exception as e:  # noqa: BLE001 - offline: show what is installed anyway
                apps, err = [], str(e) or type(e).__name__
            lay = self.layout_
            inst = osc.installed_apps(lay) if lay else []
            installed: dict[str, osc.InstalledApp] = {}
            for i in inst:
                a = osc.match_app(i, apps)
                key = a.slug if a else "dir:" + i.folder
                # a misplaced (nested) copy never hides the real install of the same app
                if key in installed and (i.nested or not installed[key].nested):
                    key = "dir:" + str(i.path)
                installed[key] = i
            updates = {a.slug: a for a in osc.update_available(inst, apps).values()}
        except Exception as e:  # noqa: BLE001 - an unreadable drive must not close the app
            self.scan_failed("Reading the apps", e)
            return
        self.from_thread(self._set, apps, installed, updates, err)

    def _set(self, apps: list[osc.App], installed: dict[str, osc.InstalledApp],
             updates: dict[str, osc.App], err: str) -> None:
        self.apps, self.installed, self.updates = apps, installed, updates
        cats = sorted({a.category for a in apps if a.category})
        if cats != self._cats:           # only rebuild the dropdown when it changed (keeps the choice)
            self._cats = cats
            sel = self.query_one("#cat", Select)
            with sel.prevent(Select.Changed):
                sel.set_options([("All categories", "")] + [(c.title(), c) for c in cats])
                if self.cat in cats:
                    sel.value = self.cat
                else:
                    self.cat = None
        if err:
            self.app.notify(f"Couldn't load the app list: {err}", severity="error")
        self._fill()

    def _platform(self) -> str:
        return self.settings.console_type or ""

    def _fill(self) -> None:
        rows = []
        for a in osc.search(self.apps, self.text, self.cat or None, self._platform() or None):
            inst = self.installed.get(a.slug)
            if self.only_installed and not inst:
                continue
            ver = inst.version if inst else ""
            if a.slug in self.updates:
                ver += "  (update)"
            rows.append((a.slug, [a.name, a.category, a.version, ver or "-",
                                  human_size(a.file_size), a.author[:20]]))
        if self.only_installed:
            words = self.text.casefold().split()
            for k, i in self.installed.items():
                if k.startswith("dir:") and all(w in i.display_name.casefold() for w in words):
                    rows.append((k, [i.display_name, "(not from the shop)" if not i.nested else "(misplaced copy)",
                                     "", i.version or "?", "", i.coder[:20]]))
        self.table.fill(rows)
        n_up = len(self.updates)
        self.query_one("#summary", Static).update(
            f"{len(self.apps)} apps available · {len(self.installed)} installed" +
            (f" · {n_up} update(s)" if n_up else "") + ("" if self.layout_ else " · no drive selected"))

    def _refilter(self) -> None:
        self.table.clear_marks()       # ticks never apply to rows you can no longer see
        self._fill()

    @on(Input.Changed, "#q")
    def _q(self, ev: Input.Changed) -> None:
        if ev.value != self.text:
            self.text = ev.value
            self._refilter()

    @on(Select.Changed, "#cat")
    def _c(self, ev: Select.Changed) -> None:
        new = str(ev.value) if isinstance(ev.value, str) and ev.value else None
        if new != self.cat:
            self.cat = new
            self._refilter()

    @on(Select.Changed, "#show")
    def _s(self, ev: Select.Changed) -> None:
        new = ev.value == "inst"
        if new != self.only_installed:
            self.only_installed = new
            self._refilter()

    def _sel_apps(self) -> list[osc.App]:
        return [a for a in (osc.find_app(self.apps, k) for k in self.table.selected_keys()) if a]

    def _cur_app(self) -> Optional[osc.App]:
        k = self.table.current_key()
        return osc.find_app(self.apps, k) if k else None

    @on(DataTable.RowSelected)
    def _row(self) -> None:
        self.action_details()

    @work(exclusive=True, group="hb")
    async def action_details(self) -> None:
        a = self._cur_app()
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
        layout = self.need_drive()
        if layout is None or not apps:
            return

        def run(progress: Any, cancel: Any) -> tuple[list[str], list[str]]:
            ok, errs = [], []
            for a in apps:
                try:
                    osc.install_app(layout, a, progress, cancel)
                    ok.append(a.name)
                except Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001 - reported per app
                    errs.append(f"{a.name}: {e or type(e).__name__}")
            return ok, errs

        res = await self.job(f"Installing {len(apps)} app(s)", run, quiet=True)
        if res is FAILED:
            self._load(False)
            return
        ok, errs = res
        if errs:
            await self.message("Some apps weren't installed", "\n".join(errs), error=True)
        else:
            self.table.clear_marks()
        self.app.notify(f"Installed {len(ok)} app(s)")
        self._load(False)

    @work(exclusive=True, group="hb")
    async def action_install(self) -> None:
        apps = self._sel_apps()
        if not apps:
            self.app.notify("Select apps from the shop to install", severity="warning")
            return
        if self.need_drive() is None:
            return
        nand = [a.name for a in apps if a.writes_to_nand]
        if nand and not await self.confirm("Heads up", "These apps change the Wii's system memory when "
                                           "you run them:\n" + titles(nand) +
                                           "\n\nMake sure you have a NAND backup first. Install anyway?",
                                           yes="Install"):
            return
        if len(apps) > 1 and not await self.confirm("Install apps", f"Install {len(apps)} apps?\n\n" +
                                                    titles([a.name for a in apps]), yes="Install"):
            return
        await self._install(apps)

    @work(exclusive=True, group="hb")
    async def action_updall(self) -> None:
        if not self.updates:
            self.app.notify("Everything is up to date")
            return
        ups = list(self.updates.values())
        if await self.confirm("Update all", "Update:\n" + titles([a.name for a in ups]), yes="Update"):
            await self._install(ups)

    @work(exclusive=True, group="hb")
    async def action_remove(self) -> None:
        layout = self.need_drive()
        if layout is None:
            return
        targets = [self.installed[k] for k in self.table.selected_keys() if k in self.installed]
        if not targets:
            self.app.notify("Select installed apps", severity="warning")
            return
        if any(i.nested for i in targets):
            self.app.notify("Misplaced copies are fixed in Drive → Check drive, not removed here",
                            severity="warning")
            return
        if not await self.confirm("Remove apps", f"Remove {len(targets)} app(s) from the drive?\n\n" +
                                  titles([i.display_name for i in targets]), danger=True, yes="Remove"):
            return

        def run(progress: Any, cancel: Any) -> list[str]:
            rep = Reporter(progress, cancel, total=len(targets))
            errs = []
            for i in targets:
                rep.note(f"Removing {i.display_name}")
                try:
                    osc.remove_app(layout, i.folder)
                except Exception as e:  # noqa: BLE001 - reported per app
                    errs.append(f"{i.display_name}: {e or type(e).__name__}")
                rep.advance(1)
            return errs

        errs = await self.job("Removing apps", run, cancellable=False, quiet=True)
        if errs is FAILED:
            self._load(False)
            return
        if errs:
            await self.message("Some apps couldn't be removed", "\n".join(errs), error=True)
        else:
            self.table.clear_marks()
        self._load(False)

    @work(exclusive=True, group="hb")
    async def action_kit(self) -> None:
        layout = self.need_drive()
        if layout is None:
            return
        plat = self._platform()
        ess = osc.starter_kit(plat, "essential")
        rec = [x for x in osc.starter_kit(plat, "all") if x not in ess]
        body = ("Essential:\n" + "\n".join(f"  {x.slug} – {x.why}" for x in ess) +
                "\n\nRecommended extras:\n" + "\n".join(f"  {x.slug} – {x.why}" for x in rec))
        if not plat:
            body += "\n\n(Tip: set your console type in Settings for the right cIOS installer.)"
        choice = await self.choose("Starter kit", body + "\n\nInstall onto the drive:",
                                   a="Essential + extras", b="Essential only")
        if choice is None:
            return
        which = "all" if choice == "a" else "essential"
        apps = self.apps or None
        res = await self.job("Installing the starter kit",
                             lambda p, c: osc.install_starter_kit(layout, plat, which, p, c, apps=apps),
                             quiet=True)
        if res is not FAILED:
            lines, bad = kit_lines(res)
            await self.message("Starter kit", "\n\n".join(lines), error=bad)
        self._load(False)

    @work(exclusive=True, group="hb")
    async def action_wiiload(self) -> None:
        a = self._cur_app()
        if not a:
            self.app.notify("Highlight an app from the shop", severity="warning")
            return
        ip = await self.prompt("Send to Wii", f"Send {a.name} straight to the Wii over the network.\n"
                               "Open the Homebrew Channel on the Wii, press HOME to see its "
                               "IP address, and enter it here:", self.settings.wii_ip)
        if not ip or not ip.strip():
            return
        host = ip.strip()
        if host != self.settings.wii_ip:
            self.settings.wii_ip = host
            self.rapp.save_settings()
        from ...core import wiiload

        def run(progress: Any, cancel: Any) -> None:
            z = osc.download_archive(a, progress, cancel)
            wiiload.send(host, z, progress=progress, cancel=cancel)

        await self.job(f"Sending {a.name} to the Wii", run)
