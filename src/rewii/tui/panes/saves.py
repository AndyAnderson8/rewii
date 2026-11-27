"""Saves: back up game saves from the drive and restore any version."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from collections.abc import Callable

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Label, Static

from ...core import saves as SV
from ..base import FAILED, Pane
from ..widgets import human_size

APP_NAMES = {SV.APP_SGM: "SaveGame Manager GX", SV.APP_SGM_LEGACY: "SaveGame Manager (old)",
             SV.APP_DATA_MGMT: "Wii Data Management", SV.APP_NINTENDONT: "Nintendont",
             SV.APP_GCMM: "GCMM"}


class SavesPane(Pane):
    DEFAULT_CSS = """
    SavesPane #drive { height: 1fr; }
    SavesPane #pc { height: 1fr; }
    """

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.drive_saves: list[SV.DriveSave] = []
        self.versions: list[SV.SaveVersion] = []

    def compose(self) -> ComposeResult:
        yield Label("Saves", classes="title")
        yield Static("Wii saves come from SaveGame Manager GX (extract them on the Wii first); GameCube saves "
                     "from Nintendont's memory cards. Backups keep every version on this computer.", classes="hint")
        with Horizontal(classes="toolbar"):
            yield Button("Back up all", id="all", variant="primary")
            yield Button("Back up selected", id="one")
            yield Button("Export GameCube save (.gci)", id="gci")
            yield Button("How to get saves off the Wii", id="help")
        yield Label("On the drive")
        yield DataTable(id="drive", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="toolbar"):
            yield Button("Restore to drive", id="restore")
            yield Button("Open backups folder", id="open")
            yield Button("Delete version", id="del")
        yield Label("Backups on this computer")
        yield DataTable(id="pc", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        self.query_one("#drive", DataTable).add_columns("Game", "ID", "Type", "From", "Size", "Changed", "Backed up")
        self.query_one("#pc", DataTable).add_columns("Game", "ID", "Type", "For", "Size", "Saved on")

    def reload(self) -> None:
        self._scan()

    @work(thread=True, exclusive=True, group="sv-scan")
    def _scan(self) -> None:
        try:
            lay = self.layout_
            ds = SV.discover_saves(lay) if lay and lay.root.exists() else []
            vs = SV.list_backups(self.settings)
        except Exception as e:  # noqa: BLE001 - an unreadable folder must not close the app
            self.scan_failed("Reading the saves", e)
            return
        self.from_thread(self._fill, ds, vs)

    def _fill(self, ds: list[SV.DriveSave], vs: list[SV.SaveVersion]) -> None:
        self.drive_saves, self.versions = ds, vs
        backed: dict[str, str] = {}
        for v in vs:
            backed[v.game_id] = max(backed.get(v.game_id, ""), v.created)
        t = self.query_one("#drive", DataTable)
        t.clear()
        for i, s in enumerate(ds):
            title = s.title + (f" ({len(s.games)} games)" if len(s.games) > 1 else "")
            t.add_row(title, s.game_id, s.kind, APP_NAMES.get(s.app, s.app), human_size(s.size),
                      datetime.fromtimestamp(s.mtime).strftime("%Y-%m-%d %H:%M"),
                      backed.get(s.game_id, "-")[:16], key=str(i))
        p = self.query_one("#pc", DataTable)
        p.clear()
        for i, v in enumerate(vs):
            p.add_row(v.title, v.game_id, v.kind, APP_NAMES.get(v.app, v.app), human_size(v.size),
                      v.created[:16], key=str(i))

    def _drive_save(self) -> SV.DriveSave | None:
        i = self.cursor_index("#drive")
        return self.drive_saves[i] if i is not None and i < len(self.drive_saves) else None

    def _version(self) -> SV.SaveVersion | None:
        i = self.cursor_index("#pc")
        return self.versions[i] if i is not None and i < len(self.versions) else None

    def button_actions(self) -> dict[str, Callable[[], Any]]:
        return {"all": self.action_all, "one": self.action_one, "gci": self.action_gci, "help": self.action_help,
                "restore": self.action_restore, "open": self.action_open, "del": self.action_del}

    def action_help(self) -> None:
        self.rapp.action_section("system")
        self.rapp.pane("system").show_guide("saves")  # type: ignore[attr-defined]

    async def _backup(self, saves: list[SV.DriveSave]) -> None:
        s = self.settings

        def run(progress: Any, cancel: Any) -> list[SV.BackupResult]:
            return [SV.backup_save(x, s, progress=progress, cancel=cancel) for x in saves]

        res = await self.job("Backing up saves", run, quiet=True)
        if res is not FAILED:
            new = sum(1 for r in res if r.created)
            self.app.notify(f"{new} new save version(s) backed up" + ("" if new else " - nothing changed"))
            self.reload()

    @work(exclusive=True, group="sv")
    async def action_all(self) -> None:
        if self.need_drive() is None:
            return
        if not self.drive_saves:
            await self.message("No saves found", "No saves on the drive yet. On the Wii, use SaveGame Manager GX "
                               "to extract saves to the drive (System & Guides → Back up and restore game saves).")
            return
        await self._backup(self.drive_saves)

    @work(exclusive=True, group="sv")
    async def action_one(self) -> None:
        sv = self._drive_save()
        if sv is None:
            self.app.notify("Highlight a save in the top list", severity="warning")
            return
        await self._backup([sv])

    @work(exclusive=True, group="sv")
    async def action_gci(self) -> None:
        sv = self._drive_save()
        if sv is None or sv.kind != SV.KIND_MEMCARD:
            self.app.notify("Select a Nintendont memory card (.raw)", severity="warning")
            return
        dest = self.settings.saves_dir / "GCI exports"

        def run(progress: Any, cancel: Any) -> list[str]:
            card = SV.parse_memcard(sv.path)
            dest.mkdir(parents=True, exist_ok=True)
            return [SV.export_gci(sv.path, n, dest).name for n in range(len(card.saves))]

        out = await self.job("Exporting GameCube saves", run, cancellable=False, quiet=True)
        if out is FAILED:
            return
        if not out:
            await self.message("Nothing to export", "That memory card has no saves on it.")
            return
        await self.message("Exported", "\n".join(out) + f"\n\nin {dest}")

    @work(exclusive=True, group="sv")
    async def action_restore(self) -> None:
        layout = self.need_drive()
        if layout is None:
            return
        v = self._version()
        if v is None:
            self.app.notify("Highlight a backup in the bottom list", severity="warning")
            return
        target = v.app
        if v.app in (SV.APP_SGM, SV.APP_SGM_LEGACY, SV.APP_DATA_MGMT) and v.path.is_file():
            ch = await self.choose("Restore for which app?",
                                   "Restore this Wii save for SaveGame Manager GX (on the USB drive) or for "
                                   "the Wii Menu's Data Management (needs an SD card)?",
                                   a="SaveGame Manager GX", b="Data Management (SD)")
            if ch is None:
                return
            target = SV.APP_SGM if ch == "a" else SV.APP_DATA_MGMT
        root = layout.root
        if target == SV.APP_DATA_MGMT:
            picked = await self.pick("Choose the SD card", folder=True)
            if not picked:
                return
            root = picked[0]
        try:
            dest = SV.restore_destination(v, root, target)
        except Exception as e:  # noqa: BLE001 - shown to the user
            await self.message("Can't restore this save", str(e) or type(e).__name__, error=True)
            return
        if not await self.confirm("Restore save", f"Copy the {v.created[:16]} version of {v.title} to\n  {dest}\n\n"
                                  "If a different save is there, it is backed up first.", yes="Restore"):
            return
        s = self.settings
        res = await self.job("Restoring", lambda p, c: SV.restore_save(v, root, target, s, progress=p, cancel=c),
                             quiet=True)
        if res is not FAILED:
            msg = "Restored." if res.copied else "That save was already on the drive."
            if target in (SV.APP_SGM, SV.APP_SGM_LEGACY):
                msg += "\n\nOn the Wii: SaveGame Manager GX → select the save → Install."
            elif target == SV.APP_DATA_MGMT:
                msg += "\n\nOn the Wii: Wii Options → Data Management → Save Data → SD Card → copy it to the Wii."
            await self.message("Restore", msg)
            self.reload()

    def action_open(self) -> None:
        import subprocess
        import sys
        p = self.settings.saves_dir
        try:
            if sys.platform == "win32":
                import os
                os.startfile(p)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            self.app.notify(str(p))

    @work(exclusive=True, group="sv")
    async def action_del(self) -> None:
        v = self._version()
        if v is None:
            self.app.notify("Highlight a backup in the bottom list", severity="warning")
            return
        if not await self.confirm("Delete backup", f"Delete the {v.created[:16]} backup of {v.title} "
                                  f"[{v.game_id}] from this computer?", danger=True, yes="Delete"):
            return
        base = self.settings.saves_dir

        def run(progress: Any, cancel: Any) -> None:
            import shutil
            if base.resolve() not in v.dir.resolve().parents:
                raise OSError(f"{v.dir} is not inside the saves folder; nothing was deleted")
            shutil.rmtree(v.dir)

        await self.job("Deleting backup", run, cancellable=False, quiet=True)
        self.reload()
