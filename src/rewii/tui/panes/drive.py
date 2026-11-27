"""Drive: choose the Wii's USB drive, check it, format it, set it up."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from collections.abc import Callable

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Label, Static

from ...core import drives as DR
from ...core import formatting as FM
from ...core import health as H
from ...core.findings import Fix
from ..base import FAILED, Pane, severity_icon
from ..widgets import cursor_key, human_size

#: Health fixes that aren't automatic: what the "Fix" column says (see ``action_fix``).
FIX_LABELS = {Fix.FORMAT_DRIVE: "Prepare", Fix.INSTALL_STARTER_APPS: "Set up",
              Fix.CONVERT_GAME: "Games → Add", Fix.IMPORT_WBFS: "Games → Import"}


class DrivePane(Pane):
    DEFAULT_CSS = """
    DrivePane #drives { height: 10; }
    DrivePane #findings { height: 1fr; }
    """

    def __init__(self, **kw: Any) -> None:
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
        self.from_thread(self._fill_drives, ds)

    def _fill_drives(self, ds: list[DR.Drive]) -> None:
        t = self.query_one("#drives", DataTable)
        before = self._cur_drive()
        self.drives = ds
        t.clear()
        seen: set[str] = set()
        for d in ds:
            if d.id in seen:        # two listings of one device must not crash the table
                continue
            seen.add(d.id)
            t.add_row(d.display_name, human_size(d.size), d.filesystem or "?", d.partition_style or "?",
                      d.mount_point or "(not mounted)", human_size(d.free) if d.free else "-",
                      ", ".join(d.notes), key=d.id)
        ds = [d for d in ds if d.id in seen]
        # keep the highlight on the same physical drive across rescans (never silently jump to row 0)
        keep = before or self._current_listed()
        want = keep.id if keep else None
        ids = [r.key.value for r in t.ordered_rows]
        if want in ids:
            t.move_cursor(row=ids.index(want))
        cur = self.layout_
        self.query_one("#current", Label).update(
            f"Current drive: [b]{cur.root}[/b]" if cur else
            "No drive selected. Plug in the Wii's USB drive and pick it above"
            + ("" if ds else " (none found - try Rescan, or 'Use a folder…')") + ".")

    def _cur_drive(self) -> DR.Drive | None:
        """The drive highlighted in the list."""
        key = cursor_key(self.query_one("#drives", DataTable))
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

    def button_actions(self) -> dict[str, Callable[[], Any]]:
        return {"use": self.action_use, "folder": self.action_folder, "format": self.action_format_selected,
                "setup": self.action_setup, "rescan": self._scan, "check": self._check,
                "fix": self.action_fix, "fixall": self.action_fixall}

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
            root = p[0]
            info = await self.bg("Looking at that folder", lambda: DR.drive_for_path(root))
            self.rapp.set_drive(root, None if info is FAILED else info)
            self.reload()

    # ------------------------------------------------------------------ health
    @work(thread=True, exclusive=True, group="drv-check")
    def _check(self) -> None:
        try:
            lay = self.layout_
            if not lay:
                fs: list[H.Finding] = []
            elif not lay.root.exists():
                fs = [H.Finding("gone", H.ERROR, "The drive isn't connected.", None)]
            else:
                fs = H.check(lay, self.rapp.drive_info or DR.drive_for_path(lay.root))
        except Exception as e:  # noqa: BLE001 - an unreadable drive must not close the app
            self.scan_failed("Checking the drive", e)
            return
        self.from_thread(self._fill_findings, fs)

    def _fill_findings(self, fs: list[H.Finding]) -> None:
        self.findings = fs
        t = self.query_one("#findings", DataTable)
        t.clear()
        if not fs and self.layout_:
            t.add_row("✔", "Everything looks good.", "", key="ok")
        for i, f in enumerate(fs):
            fix = "auto" if f.auto_fixable else FIX_LABELS.get(f.fix_id or "", "")  # type: ignore[call-overload]
            t.add_row(severity_icon(f.severity), f.message, fix, key=str(i))

    def _sel_finding(self) -> H.Finding | None:
        i = self.cursor_index("#findings")
        return self.findings[i] if i is not None and i < len(self.findings) else None

    @work(exclusive=True, group="drv")
    async def action_fix(self) -> None:
        f = self._sel_finding()
        if not f:
            return
        if f.fix_id == Fix.FORMAT_DRIVE:
            # the finding is about the drive in use - never about whatever row is highlighted
            d = self._current_listed()
            if d is None:
                await self.message("Can't tell which drive this is",
                                   "ReWii can't match the folder in use to a drive in the list. Highlight the "
                                   "drive in the list above and press 'Prepare (format FAT32)…' yourself.")
                return
            self.action_format(d)
            return
        if f.fix_id == Fix.INSTALL_STARTER_APPS:
            self.action_setup()
            return
        if f.fix_id == Fix.IMPORT_WBFS:
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
        lay = self.need_drive()
        if lay is None:
            return

        look = self.rapp.title_lookup

        def run(progress: Any, cancel: Any) -> list[str]:
            log: list[str] = []
            for f in fs:
                try:
                    log += H.fix(lay, f, progress, cancel, lookup=look)
                except (H.FixError, OSError) as e:
                    log.append(f"{H.NOT_FIXED} {self.explain(e)}")
            return log

        log = await self.job("Fixing", run)
        if log is not FAILED and log:
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

        def run(progress: Any, cancel: Any) -> Any:
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
        if res is FAILED:
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
        if new and new.mount_point:
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
        lay = self.need_drive()
        if lay is None:
            return
        from ...core import setup
        s = self.settings
        ess = setup.planned_apps(s)
        if not await self.confirm("Set up the drive",
                                  f"Creates the ReWii folders on {lay.root} and installs:\n  " +
                                  "\n  ".join(f"{x.slug} – {x.why}" for x in ess) +
                                  "\n\nand sets USB Loader GX's online server.", yes="Set up"):
            return
        res = await self.job("Setting up the drive", lambda p, c: setup.setup_drive(lay, s, p, c), quiet=True)
        if res is not FAILED:
            lines, bad = res.lines()
            await self.message("Drive set up" if not bad else "Drive set up - with problems",
                               "\n\n".join(lines), error=bad)
        self.reload()
