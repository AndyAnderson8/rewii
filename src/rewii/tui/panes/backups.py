"""Backups: whole-drive snapshots, drive cloning, and Wii NAND backups."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from collections.abc import Callable

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Label, Static

from ...core import backup as BK
from ...core import nand as NA
from ..base import FAILED, Pane
from ..widgets import human_size


class BackupsPane(Pane):
    DEFAULT_CSS = """
    BackupsPane #snaps { height: 1fr; }
    BackupsPane #nands { height: 1fr; }
    """

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.snaps: list[BK.Snapshot] = []
        self.nands: list[NA.ArchivedNand] = []

    def compose(self) -> ComposeResult:
        yield Label("Backups", classes="title")
        yield Label("Drive backups")
        with Horizontal(classes="toolbar"):
            yield Button("Back up drive", id="snap", variant="primary")
            yield Button("Restore to drive…", id="restore")
            yield Button("Compare with drive", id="diff")
            yield Button("Verify", id="verify")
            yield Button("Delete", id="delsnap")
            yield Button("Copy drive to new drive…", id="clone")
        yield DataTable(id="snaps", cursor_type="row", zebra_stripes=True)
        yield Label("Wii NAND backups (the Wii's internal memory - your unbrick insurance)")
        with Horizontal(classes="toolbar"):
            yield Button("Collect from drive", id="collect", variant="primary")
            yield Button("Add from folder…", id="addnand")
            yield Button("Verify", id="nverify")
            yield Button("Make SD rescue kit…", id="kit")
            yield Button("How to make one", id="nhelp")
        yield DataTable(id="nands", cursor_type="row", zebra_stripes=True)
        yield Static("", id="summary")

    def on_mount(self) -> None:
        self.query_one("#snaps", DataTable).add_columns("Backup", "Taken", "Files", "Size", "Games included")
        self.query_one("#nands", DataTable).add_columns("Console", "Type", "Taken", "Made with", "Files")

    def reload(self) -> None:
        self._scan()

    @work(thread=True, exclusive=True, group="bk-scan")
    def _scan(self) -> None:
        try:
            snaps = BK.list_snapshots(self.settings)
            nands = NA.list_archives(self.settings)
        except Exception as e:  # noqa: BLE001 - an unreadable folder must not close the app
            self.scan_failed("Reading the backups", e)
            return
        self.from_thread(self._fill, snaps, nands)

    def _fill(self, snaps: list[BK.Snapshot], nands: list[NA.ArchivedNand]) -> None:
        self.snaps, self.nands = snaps, nands
        t = self.query_one("#snaps", DataTable)
        t.clear()
        for i, s in enumerate(snaps):
            t.add_row(s.label, s.created[:16], str(len(s.files)), human_size(s.size),
                      "yes" if s.include_games else "no", key=str(i))
        n = self.query_one("#nands", DataTable)
        n.clear()
        for i, a in enumerate(nands):
            n.add_row(a.console_id, "Wii U (vWii)" if a.kind == "vwii" else "Wii", a.created[:16],
                      a.tool or "?", ", ".join(sorted(a.files))[:60], key=str(i))
        self.query_one("#summary", Static).update(
            f"Stored in {self.settings.library / 'Backups'}" +
            ("" if nands else "   ·   No NAND backup yet - make one before changing anything on the Wii!"))

    def _snap(self) -> BK.Snapshot | None:
        i = self.cursor_index("#snaps")
        if i is None or i >= len(self.snaps):
            self.app.notify("Highlight a drive backup first", severity="warning")
            return None
        return self.snaps[i]

    def _nand(self) -> NA.ArchivedNand | None:
        i = self.cursor_index("#nands")
        if i is None or i >= len(self.nands):
            self.app.notify("Highlight a NAND backup first", severity="warning")
            return None
        return self.nands[i]

    def button_actions(self) -> dict[str, Callable[[], Any]]:
        return {"snap": self.action_snap, "restore": self.action_restore, "diff": self.action_diff,
                "verify": self.action_verify, "delsnap": self.action_delsnap, "clone": self.action_clone,
                "collect": self.action_collect, "addnand": self.action_addnand, "nverify": self.action_nverify,
                "kit": self.action_kit, "nhelp": self.action_nhelp}

    # ------------------------------------------------------------------ drive snapshots
    @work(exclusive=True, group="bk")
    async def action_snap(self) -> None:
        lay = self.need_drive()
        if lay is None:
            return
        s = self.settings
        ch = await self.choose("Back up drive", "Include games? Games are big and you probably have them in "
                               "your library already. Without them the backup is apps, saves, covers, "
                               "settings and WADs - usually small.",
                               a="Everything but games", b="Include games")
        if ch is None:
            return
        games = ch == "b"
        root = lay.root
        res = await self.job("Backing up drive",
                             lambda p, c: BK.create_snapshot(root, s, BK.suggest_label(root), include_games=games,
                                                             progress=p, cancel=c), quiet=True)
        if res is not FAILED:
            self.app.notify(f"Backed up {len(res.files)} files ({human_size(res.size)})")
            self.reload()

    @work(exclusive=True, group="bk")
    async def action_restore(self) -> None:
        lay = self.need_drive()
        snap = self._snap() if lay is not None else None
        if lay is None or snap is None:
            return
        target = lay.root
        if not await self.confirm(
                "Restore backup", f"Copy the files from the {snap.created[:16]} backup to {target}?\n\n"
                "Files that are already the same are skipped. Files that changed since the backup "
                "(saves, settings) are REPLACED with the older backed-up version. Files that are only "
                "on the drive are left alone.", yes="Restore"):
            return
        res = await self.job("Restoring", lambda p, c: BK.restore_snapshot(snap, target, progress=p, cancel=c),
                             quiet=True)
        if res is not FAILED:
            await self.message("Restored", f"{len(res.copied)} files copied, {len(res.skipped)} already up to date."
                               + (f"\n{len(res.failed_verify)} failed verification!" if res.failed_verify else ""),
                               error=bool(res.failed_verify))

    @work(exclusive=True, group="bk")
    async def action_diff(self) -> None:
        lay = self.need_drive()
        snap = self._snap() if lay is not None else None
        if lay is None or snap is None:
            return
        root = lay.root
        d = await self.job("Comparing", lambda p, c: BK.diff_snapshot(snap, root, cancel=c), quiet=True)
        if d is FAILED:
            return
        if d.identical:
            await self.message("Compare", "The drive matches this backup.")
            return
        parts = []
        for name, lst in (("New on the drive", d.added), ("Missing from the drive", d.removed),
                          ("Changed", d.changed)):
            if lst:
                parts.append(f"{name} ({len(lst)}):\n  " + "\n  ".join(lst[:40]) + ("\n  …" if len(lst) > 40 else ""))
        await self.message("Compare", "\n\n".join(parts))

    @work(exclusive=True, group="bk")
    async def action_verify(self) -> None:
        snap = self._snap()
        if snap is None:
            return
        bad = await self.job("Verifying backup", lambda p, c: BK.verify_snapshot(snap, p, c), quiet=True)
        if bad is not FAILED:
            await self.message("Verify", "Backup is intact." if not bad else "Problems:\n" + "\n".join(map(str, bad)),
                               error=bool(bad))

    @work(exclusive=True, group="bk")
    async def action_delsnap(self) -> None:
        snap = self._snap()
        if snap is None:
            return
        if await self.confirm("Delete backup", f"Delete the {snap.created[:16]} backup of {snap.label} "
                              f"({len(snap.files)} files, {human_size(snap.size)}) from this computer?",
                              danger=True, yes="Delete"):
            await self.job("Deleting backup", lambda p, c: BK.delete_snapshot(snap), cancellable=False, quiet=True)
            self.reload()

    @work(exclusive=True, group="bk")
    async def action_clone(self) -> None:
        lay = self.need_drive()
        if lay is None:
            return
        src = lay.root
        picked = await self.pick("Choose the NEW drive (already formatted FAT32)", folder=True)
        if not picked:
            return
        dst = picked[0]
        try:
            same = dst.resolve() == src.resolve()
        except OSError:
            same = False
        if same:
            self.app.notify("That's the same drive", severity="error")
            return
        if not await self.confirm("Copy drive", f"Copy everything from {src} to {dst} and check each file?\n\n"
                                  "Files with the same name on the new drive are replaced; other files "
                                  "there are left alone.", yes="Copy"):
            return
        res = await self.job("Copying drive", lambda p, c: BK.clone_drive(src, dst, progress=p, cancel=c),
                             quiet=True)
        if res is not FAILED:
            await self.message("Copied", f"{len(res.copied)} files copied ({human_size(res.bytes_copied)})." +
                               (f"\n{len(res.failed_verify)} failed verification!" if res.failed_verify else ""),
                               error=bool(res.failed_verify))
            if await self.confirm("Switch drive", f"Use {dst} as the Wii drive from now on?"):
                self.rapp.set_drive(dst)

    # ------------------------------------------------------------------ NAND
    def action_nhelp(self) -> None:
        self.rapp.action_section("system")
        self.rapp.pane("system").show_guide("nand-backup")  # type: ignore[attr-defined]

    async def _archive(self, root: Path) -> None:
        def look(progress: Any, cancel: Any) -> tuple[list[NA.NandDump], list[NA.VwiiDump], list[str]]:
            # hashing a 512 MB nand.bin takes a while: do it here, with a progress bar
            dumps = NA.find_nand_dumps(root)
            vdumps = NA.find_vwii_dumps(root)
            lines = []
            for d in dumps:
                v = NA.validate_dump(d, progress=progress, cancel=cancel)
                lines.append(f"{d.nand.name} ({d.tool}): {'OK' if v.ok else 'PROBLEM'} – console {v.console_label}")
                lines += [f"   ✖ {e}" for e in v.errors] + [f"   ! {w}" for w in v.warnings]
            for vd in vdumps:
                errs = NA.verify_vwii_dump(vd)
                lines.append(f"Wii U NAND dump: {'OK' if not errs else 'PROBLEM'}")
                lines += [f"   ✖ {e}" for e in errs]
            return dumps, vdumps, lines

        found = await self.job("Checking the NAND backup", look, quiet=True)
        if found is FAILED:
            return
        dumps, vdumps, lines = found
        if not dumps and not vdumps:
            await self.message("No NAND backup found", f"Nothing found in {root}. Run nanddumper@ios on the Wii first "
                               "(System & Guides → Back up your NAND).")
            return
        s = self.settings
        if not await self.confirm("Save NAND backup", "\n".join(lines) + "\n\nSave to your library?", yes="Save"):
            return

        def run(progress: Any, cancel: Any) -> list[tuple[NA.ArchivedNand, bool]]:
            out = []
            for d in dumps:
                out.append(NA.archive_dump(d, s, progress=progress, cancel=cancel))
            for vd in vdumps:
                out.append((NA.archive_vwii_dump(vd, s, progress=progress, cancel=cancel), True))
            return out

        res = await self.job("Saving NAND backup", run, quiet=True)
        if res is not FAILED:
            new = sum(1 for _, created in res if created)
            await self.message("NAND backup saved",
                               (f"{new} backup(s) saved" if new else "Already saved before") +
                               f" in {s.nand_backups_dir}.\n\nKeep a second copy somewhere else too "
                               "(cloud storage or another disk).")
            self.reload()

    @work(exclusive=True, group="bk")
    async def action_collect(self) -> None:
        lay = self.need_drive()
        if lay is not None:
            await self._archive(lay.root)

    @work(exclusive=True, group="bk")
    async def action_addnand(self) -> None:
        picked = await self.pick("Folder containing nand.bin and keys.bin", folder=True)
        if picked:
            await self._archive(picked[0])

    @work(exclusive=True, group="bk")
    async def action_nverify(self) -> None:
        a = self._nand()
        if a is None:
            return
        bad = await self.job("Verifying NAND backup", lambda p, c: NA.verify_archive(a, p, c), quiet=True)
        if bad is not FAILED:
            await self.message("Verify", "Backup is intact." if not bad else "\n".join(bad), error=bool(bad))

    @work(exclusive=True, group="bk")
    async def action_kit(self) -> None:
        a = self._nand()
        if a is None:
            return
        if not await self.confirm(
                "SD rescue kit",
                f"This puts the NAND backup of console {a.console_id} and BootMii on an SD card, so BootMii can "
                "restore it.\n\nONLY use it on that same Wii. Restoring another Wii's backup bricks it.\n\n"
                "Next: choose the SD card.", danger=True):
            return
        picked = await self.pick("Choose the SD card", folder=True)
        if not picked:
            return
        target = picked[0]
        bootmii_dir = None
        if a.bootmii_dir is None:
            if not await self.confirm("BootMii files", "This backup has no BootMii files. Download them "
                                      "(from wii.hacks.guide, checked against known hashes)?"):
                return
            dl = self.settings.downloads_dir / "bootmii"
            bootmii_dir = await self.job("Downloading BootMii", lambda p, c: NA.fetch_bootmii_sd_files(
                dl, progress=p, cancel=c))
            if bootmii_dir is FAILED:
                return
        # a keys.bin already on the card tells us which Wii the card belongs to: let the
        # kit builder refuse if it is a different console than this backup
        tk = target / "keys.bin"
        kit = await self.job("Making rescue kit", lambda p, c: NA.build_rescue_kit(
            a, target, bootmii_dir=bootmii_dir, target_keys=tk if tk.is_file() else None,
            progress=p, cancel=c))
        if kit is not FAILED:
            await self.message("Rescue kit ready", "\n".join(str(f) for f in kit.files) + "\n\n" +
                               "\n".join(kit.warnings) + "\n\nSee System & Guides → If something goes wrong.")
