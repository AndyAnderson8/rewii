"""Saves: back up game saves from the drive and restore any version."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Label, Static

from ...core import saves as SV
from ..base import Pane
from ..widgets import human_size

APP_NAMES = {SV.APP_SGM: "SaveGame Manager GX", SV.APP_SGM_LEGACY: "SaveGame Manager (old)",
             SV.APP_DATA_MGMT: "Wii Data Management", SV.APP_NINTENDONT: "Nintendont",
             SV.APP_GCMM: "GCMM"}


class SavesPane(Pane):
    TITLE = "Saves"
    DEFAULT_CSS = """
    SavesPane #drive { height: 1fr; }
    SavesPane #pc { height: 1fr; }
    """

    def __init__(self, **kw) -> None:
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
        ds = SV.discover_saves(self.layout_.root) if self.layout_ and self.layout_.root.exists() else []
        vs = SV.list_backups(self.settings)
        self.app.call_from_thread(self._fill, ds, vs)

    def _fill(self, ds, vs) -> None:
        self.drive_saves, self.versions = ds, vs
        backed = {}
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

    def _sel(self, tid: str) -> int | None:
        t = self.query_one(tid, DataTable)
        if not t.row_count:
            return None
        return int(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)

    @on(Button.Pressed)
    def _b(self, ev: Button.Pressed) -> None:
        {"all": self.action_all, "one": self.action_one, "gci": self.action_gci, "help": self.action_help,
         "restore": self.action_restore, "open": self.action_open, "del": self.action_del}[ev.button.id]()

    def action_help(self) -> None:
        self.rapp.action_section("system")
        self.rapp.query_one("#system").show_guide("saves")  # type: ignore[attr-defined]

    async def _backup(self, saves: list[SV.DriveSave]) -> None:
        s = self.settings

        def run(progress, cancel):
            return [SV.backup_save(x, s, progress=progress, cancel=cancel) for x in saves]

        res = await self.job("Backing up saves", run)
        if res is not None:
            new = sum(1 for r in res if r.created)
            self.app.notify(f"{new} new save version(s) backed up" + ("" if new else " - nothing changed"))
            self.reload()

    @work(exclusive=True, group="sv")
    async def action_all(self) -> None:
        if not self.need_drive():
            return
        if not self.drive_saves:
            await self.message("No saves found", "No saves on the drive yet. On the Wii, use SaveGame Manager GX "
                               "to extract saves to the drive (System & Guides → Back up and restore game saves).")
            return
        await self._backup(self.drive_saves)

    @work(exclusive=True, group="sv")
    async def action_one(self) -> None:
        i = self._sel("#drive")
        if i is not None:
            await self._backup([self.drive_saves[i]])

    @work(exclusive=True, group="sv")
    async def action_gci(self) -> None:
        i = self._sel("#drive")
        if i is None:
            return
        s = self.drive_saves[i]
        if s.kind != SV.KIND_MEMCARD:
            self.app.notify("Select a Nintendont memory card (.raw)", severity="warning")
            return
        card = SV.parse_memcard(s.path)
        dest = self.settings.saves_dir / "GCI exports"
        dest.mkdir(parents=True, exist_ok=True)
        out = [SV.export_gci(s.path, n, dest) for n in range(len(card.saves))]
        await self.message("Exported", "\n".join(p.name for p in out) + f"\n\nin {dest}")

    @work(exclusive=True, group="sv")
    async def action_restore(self) -> None:
        if not self.need_drive():
            return
        i = self._sel("#pc")
        if i is None:
            return
        v = self.versions[i]
        target = v.app
        if v.app in (SV.APP_SGM, SV.APP_SGM_LEGACY, SV.APP_DATA_MGMT) and v.path.is_file():
            use_dm = await self.confirm("Restore for which app?",
                                        "Restore this Wii save for SaveGame Manager GX (on the USB drive) or for "
                                        "the Wii Menu's Data Management (needs an SD card)?",
                                        yes="Data Management (SD)", no="SaveGame Manager GX")
            target = SV.APP_DATA_MGMT if use_dm else SV.APP_SGM
        root = self.layout_.root
        if target == SV.APP_DATA_MGMT:
            picked = await self.pick("Choose the SD card", folder=True)
            if not picked:
                return
            root = picked[0]
        dest = SV.restore_destination(v, root, target)
        if not await self.confirm("Restore save", f"Copy the {v.created[:16]} version of {v.title} to\n  {dest}\n\n"
                                  "If a different save is there, it is backed up first."):
            return
        s = self.settings
        res = await self.job("Restoring", lambda p, c: SV.restore_save(v, root, target, s, progress=p, cancel=c))
        if res is not None:
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
        i = self._sel("#pc")
        if i is None:
            return
        v = self.versions[i]
        if await self.confirm("Delete backup", f"Delete the {v.created[:16]} backup of {v.title}?", danger=True,
                              yes="Delete"):
            import shutil
            if self.settings.saves_dir.resolve() in v.dir.resolve().parents:
                shutil.rmtree(v.dir)
            self.reload()
