"""Drive: choose the Wii's USB drive, check it, format it, set it up."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Label, Static

from ...core import drives as DR
from ...core import formatting as FM
from ...core import health as H
from ..base import Pane
from ..widgets import human_size

SEV = {"error": "✖", "warn": "!", "info": "i"}


class DrivePane(Pane):
    TITLE = "Drive"
    DEFAULT_CSS = """
    DrivePane #drives { height: 10; }
    DrivePane #findings { height: 1fr; }
    """

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.drives: list[DR.Drive] = []
        self.findings: list[H.Finding] = []

    def compose(self) -> ComposeResult:
        yield Label("Drive", classes="title")
        yield Static("Pick the USB drive you use in the Wii. ReWii checks it and can format it and "
                     "install the essential apps.", classes="hint")
        with Horizontal(classes="toolbar"):
            yield Button("Use this drive", id="use", variant="primary")
            yield Button("Use a folder…", id="folder")
            yield Button("Prepare (format FAT32)…", id="format", variant="error")
            yield Button("Set up", id="setup")
            yield Button("Rescan", id="rescan")
        yield DataTable(id="drives", cursor_type="row")
        yield Label("", id="current")
        with Horizontal(classes="toolbar"):
            yield Button("Check drive", id="check")
            yield Button("Fix selected", id="fix")
            yield Button("Fix everything safe", id="fixall")
        yield DataTable(id="findings", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#drives", DataTable).add_columns("Drive", "Size", "Format", "Partitions", "Mounted at",
                                                         "Free", "Notes")
        self.query_one("#findings", DataTable).add_columns(" ", "Finding", "Fix")

    def reload(self) -> None:
        self._scan()
        self._check()

    # ------------------------------------------------------------------ drive list
    @work(thread=True, exclusive=True, group="drv-scan")
    def _scan(self) -> None:
        try:
            ds = DR.list_drives()
        except Exception:
            ds = []
        self.app.call_from_thread(self._fill_drives, ds)

    def _fill_drives(self, ds: list[DR.Drive]) -> None:
        t = self.query_one("#drives", DataTable)
        before = self._cur_drive()
        self.drives = ds
        t.clear()
        for i, d in enumerate(ds):
            t.add_row(d.display_name, human_size(d.size), d.filesystem or "?", d.partition_style or "?",
                      d.mount_point or "(not mounted)", human_size(d.free) if d.free else "-",
                      ", ".join(d.notes), key=d.id)
        # keep the highlight on the same physical drive across rescans (never silently jump to row 0)
        want = before.id if before else (self._current_listed().id if self._current_listed() else None)
        for i, d in enumerate(ds):
            if d.id == want:
                t.move_cursor(row=i)
        cur = self.layout_
        self.query_one("#current", Label).update(
            f"Current drive: [b]{cur.root}[/b]" if cur else
            "No drive selected. Plug in the Wii's USB drive and pick it above"
            + ("" if ds else " (none found - try Rescan, or 'Use a folder…')") + ".")

    def _cur_drive(self) -> DR.Drive | None:
        """The drive highlighted in the list."""
        t = self.query_one("#drives", DataTable)
        if not t.row_count:
            return None
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        return next((d for d in self.drives if d.id == key), None)

    def _current_listed(self) -> DR.Drive | None:
        """The listed drive that holds the folder ReWii is currently using, if any."""
        lay = self.layout_
        if lay is None:
            return None
        try:
            root = lay.root.resolve()
        except OSError:
            return None
        for d in self.drives:
            for mp in d.mount_points:
                try:
                    if Path(mp).resolve() == root:
                        return d
                except OSError:
                    continue
        return None

    @on(Button.Pressed)
    def _b(self, ev: Button.Pressed) -> None:
        {"use": self.action_use, "folder": self.action_folder, "format": self.action_format_selected,
         "setup": self.action_setup, "rescan": self._scan, "check": self._check,
         "fix": self.action_fix, "fixall": self.action_fixall}[ev.button.id]()

    def action_use(self) -> None:
        d = self._cur_drive()
        if not d:
            return
        if not d.mount_point:
            self.app.notify("This drive isn't mounted (it may be unformatted or WBFS). Use 'Prepare' to "
                            "format it, or import its games from Games → Import old WBFS drive.",
                            severity="warning")
            return
        self.rapp.set_drive(Path(d.mount_point), d)
        self.reload()

    @work(exclusive=True, group="drv")
    async def action_folder(self) -> None:
        p = await self.pick("Choose the drive (or any folder to use as one)", folder=True)
        if p:
            self.rapp.set_drive(p[0], DR.drive_for_path(p[0]))
            self.reload()

    # ------------------------------------------------------------------ health
    @work(thread=True, exclusive=True, group="drv-check")
    def _check(self) -> None:
        lay = self.layout_
        if not lay:
            self.app.call_from_thread(self._fill_findings, [])
            return
        if not lay.root.exists():
            self.app.call_from_thread(self._fill_findings, [H.Finding(
                "gone", "error", "The drive isn't connected.", None)])
            return
        info = self.rapp.drive_info or DR.drive_for_path(lay.root)
        self.app.call_from_thread(self._fill_findings, H.check(lay, info))

    def _fill_findings(self, fs: list[H.Finding]) -> None:
        self.findings = fs
        t = self.query_one("#findings", DataTable)
        t.clear()
        if not fs and self.layout_:
            t.add_row("✔", "Everything looks good.", "", key="ok")
        for i, f in enumerate(fs):
            fix = "auto" if f.auto_fixable else {"format_drive": "Prepare", "install_starter_apps": "Set up",
                                                  "convert_game": "Games → Add", "import_wbfs": "Games → Import"
                                                  }.get(f.fix_id or "", "")
            t.add_row(SEV.get(f.severity, "?"), f.message, fix, key=str(i))

    def _sel_finding(self) -> H.Finding | None:
        t = self.query_one("#findings", DataTable)
        if not t.row_count:
            return None
        k = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        return self.findings[int(k)] if k != "ok" else None

    @work(exclusive=True, group="drv")
    async def action_fix(self) -> None:
        f = self._sel_finding()
        if not f:
            return
        if f.fix_id == "format_drive":
            # the finding is about the drive in use - never about whatever row is highlighted
            d = self._current_listed()
            if d is None:
                await self.message("Can't tell which drive this is",
                                   "ReWii can't match the folder in use to a drive in the list. Highlight the "
                                   "drive in the list above and press 'Prepare (format FAT32)…' yourself.")
                return
            self.action_format(d)
            return
        if f.fix_id == "install_starter_apps":
            self.action_setup()
            return
        if f.fix_id == "import_wbfs":
            self.rapp.action_section("games")
            return
        if not f.auto_fixable:
            await self.message("Manual fix", f.message)
            return
        await self._apply([f])

    @work(exclusive=True, group="drv")
    async def action_fixall(self) -> None:
        fs = [f for f in self.findings if f.auto_fixable]
        if not fs:
            self.app.notify("Nothing to fix automatically")
            return
        if await self.confirm("Fix drive", "Apply these fixes?\n  " + "\n  ".join(f.message for f in fs)):
            await self._apply(fs)

    async def _apply(self, fs: list[H.Finding]) -> None:
        lay = self.layout_

        def run(progress, cancel):
            log = []
            for f in fs:
                try:
                    log += H.fix(lay, f, progress, cancel)
                except H.FixError as e:
                    log.append(f"couldn't fix: {e}")
            return log

        log = await self.job("Fixing", run)
        if log:
            await self.message("Done", "\n".join(log))
        self._check()

    # ------------------------------------------------------------------ format
    def action_format_selected(self) -> None:
        d = self._cur_drive()
        if d is None:
            self.app.notify("Highlight a drive in the list first", severity="warning")
            return
        self.action_format(d)

    @work(exclusive=True, group="drv")
    async def action_format(self, d: DR.Drive) -> None:
        try:
            plan = FM.plan_format(d)
        except FM.FormatRefused as e:
            await self.message("Can't format this drive", str(e), error=True)
            return
        body = plan.summary + "\n\n"
        if plan.warnings:
            body += "\n".join("• " + w for w in plan.warnings) + "\n\n"
        body += "EVERYTHING ON THIS DRIVE WILL BE ERASED. Copy anything you want to keep first " \
                "(Backups → Back up drive)."
        if plan.requires_admin and not FM.is_admin():
            body += "\n\nYour computer will ask for an administrator password."
        word = (d.label or human_size(d.size)).strip()
        if not await self.confirm("Erase and format", body, yes="Erase drive", danger=True,
                                  type_to_confirm=word):
            return

        def run(progress, cancel):
            if plan.is_image or not plan.requires_admin or FM.is_admin():
                if plan.is_image:
                    return FM.execute_plan(plan, d.id, progress, cancel)
                # plan again from a fresh look at the drive (mounts may have changed since the scan)
                fresh = next((x for x in DR.list_drives() if x.id == d.id), None)
                if fresh is None:
                    raise FM.FormatRefused("The drive is no longer connected. Nothing was changed.")
                return FM.execute_plan(FM.plan_format(fresh), d.id, progress, cancel)
            from ...core.progress import Reporter
            Reporter(progress, cancel).note("Formatting (approve the administrator prompt)…")
            rc = FM.run_elevated(FM.helper_args(d.id))
            if rc != 0:
                raise FM.FormatFailed("Formatting was cancelled or failed" + (f" (code {rc})" if rc > 0 else ""))
            return True

        res = await self.job("Formatting", run, cancellable=plan.is_image or FM.is_admin())
        if res is None:
            return
        # find the freshly mounted volume
        new = None
        for _ in range(10):
            await asyncio.sleep(0.5)
            for x in await asyncio.to_thread(DR.list_drives):
                if x.device == d.device and x.mount_point:
                    new = x
            if new:
                break
        if new:
            self.rapp.set_drive(Path(new.mount_point), new)
            if await self.confirm("Formatted", f"{new.display_name} is ready. Install the essential apps now?",
                                  yes="Set up", no="Later"):
                self.action_setup()
        else:
            await self.message("Formatted", "Done. If the drive doesn't show up, unplug it and plug it back in, "
                               "then press Rescan and 'Use this drive'.")
        self.reload()

    # ------------------------------------------------------------------ set up
    @work(exclusive=True, group="drv")
    async def action_setup(self) -> None:
        if not self.need_drive():
            return
        from ...core import osc
        plat = self.settings.console_type
        ess = osc.starter_kit(plat, "essential")
        if not await self.confirm("Set up the drive",
                                  "Creates the ReWii folders and installs:\n  " +
                                  "\n  ".join(f"{x.slug} – {x.why}" for x in ess) +
                                  "\n\nand sets USB Loader GX's online server."):
            return
        lay, s = self.layout_, self.settings

        def run(progress, cancel):
            lay.ensure_dirs()
            lay.write_manifest({"created_by": "ReWii", "updated": time.strftime("%Y-%m-%d %H:%M")})
            rep = osc.install_starter_kit(lay, plat, "essential", progress, cancel)
            try:
                from ...core.online import set_loader_private_server
                set_loader_private_server(lay, s.online_server)
            except Exception:
                pass
            return rep

        rep = await self.job("Setting up the drive", run)
        if rep is not None:
            lines = [f"Installed: {', '.join(rep.installed) or 'already up to date'}"]
            if rep.failed:
                lines.append("Failed: " + ", ".join(dict(rep.failed)))
            lines.append("\nNext: on the Wii, back up the NAND, install Priiloader and the cIOS "
                         "(System & Guides).")
            await self.message("Drive set up", "\n".join(lines))
        self.reload()
