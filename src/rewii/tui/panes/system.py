"""System & Guides: step-by-step guides, SysCheck, and system files (cIOS, 4.3 update, themes, mods)."""
from __future__ import annotations

from typing import Any
from collections.abc import Callable

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Label, ListItem, ListView, Markdown

from ...core import guides as GD
from ...core.findings import Fix
from ...core.layout import DriveLayout
from ..base import FAILED, Pane, kit_lines, severity_icon

# guide.prepare -> OSC slugs to install
PREPARE_APPS = {
    "nanddumper": ["nanddumper_ios"],
    "priiloader": ["priiloader", "LoadPriiloader"],
    "syscheck": ["SysCheckME"],
    "cleanrip": ["CleanRip"],
}
#: guide.prepare -> the SystemPane method that prepares it (instead of an app list)
PREPARE_ACTIONS = {"cios": "_cios", "hackmii": "_hackmii", "priiloader-hacks": "_hacks"}
#: SysCheck fix -> the guide that explains it
SYSCHECK_FIX_GUIDE: dict[str, str] = {
    Fix.INSTALL_PRIILOADER: "priiloader", Fix.INSTALL_D2X_CIOS: "cios",
    Fix.UPDATE_43: "recovery", Fix.RESTORE_IOS: "recovery", Fix.UPDATE_HBC: "update-hbc",
    Fix.INSTALL_HBC: "update-hbc", Fix.INSTALL_BOOTMII: "update-hbc"}


class SystemPane(Pane):
    DEFAULT_CSS = """
    SystemPane #glist { width: 34; height: 1fr; border-right: solid $primary-background; }
    SystemPane #gview { width: 1fr; height: 1fr; padding: 0 1; }
    SystemPane #main { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        yield Label("System & Guides", classes="title")
        with Horizontal(classes="toolbar"):
            yield Button("Prepare files for this guide", id="prepare", variant="primary")
            yield Button("Read SysCheck report", id="syscheck")
            yield Button("cIOS files", id="cios")
            yield Button("4.3 update files", id="update")
            yield Button("Themes…", id="themes")
            yield Button("Riivolution mod…", id="riivo")
        with Horizontal(classes="toolbar"):
            yield Button("Update Homebrew Channel / BootMii", id="hackmii")
            yield Button("Priiloader hacks…", id="hacks")
            yield Button("BootMii settings (SD)…", id="bootmii")
        with Horizontal(id="main"):
            yield ListView(*[ListItem(Label(("★ " if g.essential else "  ") + g.title), id=f"g-{g.id}")
                             for g in GD.GUIDES], id="glist")
            with VerticalScroll(id="gview"):
                yield Markdown("", id="md")
        self.current = GD.GUIDES[0]

    def on_mount(self) -> None:
        self._show(GD.GUIDES[0])

    def _show(self, g: GD.Guide) -> None:
        self.current = g
        body = f"# {g.title}\n\n*{g.summary}*\n\n{g.body}"
        if g.link:
            body += f"\n\nMore detail: {g.link}"
        ct = self.settings.console_type
        if ct and ct not in g.consoles:
            body = "> **Not for your console type** (see Settings).\n\n" + body
        self.query_one("#md", Markdown).update(body)
        self.query_one("#prepare", Button).disabled = not g.prepare

    @on(ListView.Highlighted, "#glist")
    def _hl(self, ev: ListView.Highlighted) -> None:
        if ev.item and ev.item.id:
            self._show(GD.get(ev.item.id[2:]))

    def show_guide(self, gid: str) -> None:
        lv = self.query_one("#glist", ListView)
        lv.index = [g.id for g in GD.GUIDES].index(gid)
        self._show(GD.get(gid))

    def button_actions(self) -> dict[str, Callable[[], Any]]:
        return {"prepare": self.action_prepare, "syscheck": self.action_syscheck, "cios": self.action_cios,
                "update": self.action_update, "themes": self.action_themes, "riivo": self.action_riivo,
                "hackmii": self.action_hackmii, "hacks": self.action_hacks, "bootmii": self.action_bootmii}

    def focus_main(self) -> None:
        self.query_one("#glist", ListView).focus()

    # ------------------------------------------------------------------ prepare
    async def _install_slugs(self, slugs: list[str]) -> bool:
        """Install apps from the shop onto the drive. True only if every one is there afterwards."""
        from ...core import osc
        lay = self.need_drive()
        if lay is None:
            return False
        plat = self.settings.console_type
        rep = await self.job("Installing apps", lambda p, c: osc.install_starter_kit(lay, plat, slugs, p, c),
                             quiet=True)
        if rep is FAILED:
            return False
        lines, bad = kit_lines(rep)
        if bad or rep.unsupported:
            await self.message("Some apps weren't installed", "\n\n".join(lines), error=True)
            return False
        return True

    @work(exclusive=True, group="sys")
    async def action_prepare(self) -> None:
        g = self.current
        if not g.prepare or self.need_drive() is None:
            return
        if g.prepare in PREPARE_ACTIONS:
            await getattr(self, PREPARE_ACTIONS[g.prepare])()
            return
        slugs = PREPARE_APPS.get(g.prepare)
        if slugs and await self._install_slugs(slugs):
            await self.message("Ready", f"The files for “{g.title}” are on the drive. Follow the steps on the Wii.")

    async def _cios(self) -> None:
        from ...core import nus
        from ...core.wad import Refused
        s, lay = self.settings, self.need_drive()
        if lay is None:
            return
        if s.console_type == "mini":
            from ...core import sysfiles
            if not await self.confirm("Wii mini cIOS", "The Wii mini uses the d2xl cIOS installer (from "
                                      "wii.hacks.guide). Put it on the drive?\n\nOn the Wii: Homebrew Channel → "
                                      "d2x cIOS Installer (mini) → base 57, slot 249 → Install."):
                return
            await self.job("Getting the d2xl installer", lambda p, c: sysfiles.stage_d2xl_mini(lay, p, c),
                           cancellable=False)
            return
        try:
            entries = nus.cios_entries(s)
        except Refused as e:
            await self.message("Console needed", f"{e}\n\nSet your console type and region in Settings.", error=True)
            return
        if not await self.confirm("cIOS files", "Download these from Nintendo's servers to the drive:\n  " +
                                  "\n  ".join(e.filename for e in entries) +
                                  "\n\nand install the d2x cIOS installer app?", yes="Download"):
            return
        res = await self.job("Downloading cIOS base files", lambda p, c: nus.prepare_cios_files(lay, s, p, c))
        if res is FAILED:
            return
        slug = "d2x-cios-installer-vwii" if s.console_type == "vwii" else "d2x-cios-installer"
        if await self._install_slugs([slug]):
            self.show_guide("cios")
            await self.message("Ready", "Now follow the cIOS guide on the Wii.")

    @work(exclusive=True, group="sys")
    async def action_cios(self) -> None:
        if self.need_drive() is not None:
            await self._cios()

    @work(exclusive=True, group="sys")
    async def action_update(self) -> None:
        lay = self.need_drive()
        if lay is None:
            return
        from ...core import nus
        from ...core.wad import Refused
        s = self.settings
        try:
            entries = nus.update_43_entries(s)
        except Refused as e:
            await self.message("Not available", str(e), error=True)
            return
        if not await self.confirm(
                "Update to System Menu 4.3",
                "Only needed if your Wii is on an older System Menu (SysCheck tells you).\n\nDownloads:\n  " +
                "\n  ".join(e.filename for e in entries) +
                "\n\ninto the drive's wad/ folder. Install them with YAWMME: the two IOS first, then the System "
                "Menu. Have Priiloader and a NAND backup first!", yes="Download"):
            return
        await self.job("Downloading update files", lambda p, c: nus.prepare_update_43(lay, s, p, c))

    # ------------------------------------------------------------------ HBC / Priiloader / BootMii
    async def _hackmii(self) -> None:
        lay = self.need_drive()
        if lay is None:
            return
        from ...core import sysfiles
        res = await self.job("Getting the HackMii installer", lambda p, c: sysfiles.stage_hackmii(lay, p, c),
                             cancellable=False)
        if res is not FAILED:
            self.show_guide("update-hbc")
            await self.message("Ready", "The HackMii installer is on the drive. Follow the steps in the guide.")

    @work(exclusive=True, group="sys")
    async def action_hackmii(self) -> None:
        await self._hackmii()

    async def _hacks(self) -> None:
        lay = self.need_drive()
        if lay is None:
            return
        from ...core import sysfiles
        from ..widgets import PromptText

        def names() -> list[str]:
            p = sysfiles.hacks_path(lay)
            return sysfiles.hack_names(p.read_text("utf-8", "replace")) if p.is_file() else []

        have = await self.bg("Reading the hack list", names)
        if have is FAILED:
            return
        ch = await self.choose(
            "Priiloader hacks",
            (f"The drive has {len(have)} hacks in apps/priiloader/hacks_hash.ini." if have else
             "No Priiloader hack list on the drive yet.") +
            "\n\nUpdate to the newest official list, or add a patch you generated?",
            a="Update list", b="Add a patch")
        if ch is None:
            return
        if ch == "a":
            res = await self.job("Updating hack list", lambda pr, c: sysfiles.update_priiloader_hacks(lay, pr, c),
                                 cancellable=False, quiet=True)
            if res is not FAILED:
                await self.message("Hack list updated", f"Priiloader {res[1]} hack list saved to {res[0]}.\n\n"
                                   "Turn hacks on in Priiloader → System Menu Hacks.")
            return
        text = await self.app.push_screen_wait(PromptText(
            "Add a Priiloader patch", "Paste the patch text (it starts with a [Name] line):"))
        if not text:
            return
        if await self.bg("Adding the patch", lambda: sysfiles.add_custom_hack(lay, text)) is FAILED:
            return
        await self.message("Patch added", "Now enable it on the Wii: hold RESET at power-on → Priiloader → "
                           "System Menu Hacks.")

    @work(exclusive=True, group="sys")
    async def action_hacks(self) -> None:
        await self._hacks()

    @work(exclusive=True, group="sys")
    async def action_bootmii(self) -> None:
        from ...core import sysfiles
        from ..widgets import ChoiceForm
        picked = await self.pick("Choose the SD card (with the bootmii folder)", folder=True)
        if not picked:
            return
        sd = DriveLayout(picked[0])
        if not sd.bootmii.is_dir():
            await self.message("No BootMii here", "That card has no bootmii folder. Make one with Backups → "
                               "Make SD rescue kit, or the HackMii installer.", error=True)
            return
        cur = await self.bg("Reading bootmii.ini", lambda: sysfiles.read_bootmii_ini(sd))
        if cur is FAILED:
            return
        fields = [
            ("AUTOBOOT", "Start automatically", [("Wii Menu", "SYSMENU"), ("Homebrew Channel", "HBC"),
                                                 ("Off (show BootMii menu)", "OFF")], cur.get("AUTOBOOT", "SYSMENU")),
            ("BOOTDELAY", "Seconds before starting", [(str(i), str(i)) for i in range(11)], cur.get("BOOTDELAY", "1")),
            ("VIDEO", "Video mode", [("NTSC", "NTSC"), ("PAL 50Hz", "PAL50"), ("PAL 60Hz", "PAL60"),
                                     ("Progressive", "PROGRESSIVE")], cur.get("VIDEO", "NTSC")),
        ]
        res = await self.app.push_screen_wait(ChoiceForm(
            "BootMii settings", "If BootMii is installed as boot2, these decide what happens at power-on. "
            "The guide recommends: Wii Menu, 1 second.", fields))
        if res:
            if await self.bg("Saving bootmii.ini", lambda: sysfiles.write_bootmii_ini(sd, res)) is not FAILED:
                self.app.notify("bootmii.ini saved")

    # ------------------------------------------------------------------ syscheck
    @work(exclusive=True, group="sys")
    async def action_syscheck(self) -> None:
        from ...core import syscheck as SC
        lay = self.layout_
        path = SC.find_syscheck(lay) if lay else None
        if path is None:
            picked = await self.pick("Find syscheck.csv", exts={".csv", ".txt"})
            if not picked:
                return
            path = picked[0]
        report = path
        r = await self.bg("Reading the SysCheck report", lambda: SC.read_syscheck(report))
        if r is FAILED:
            return
        lines = [f"System Menu: {r.sysmenu or '?'}   Region: {r.region or '?'}   Console: {r.console_type or '?'}",
                 f"Homebrew Channel: {'.'.join(map(str, r.hbc_version)) if r.hbc_version else 'not found'}   "
                 f"Priiloader: {'yes' if r.priiloader else 'no'}"]
        cios = [i for slot, i in sorted(r.ios.items()) if 200 <= slot <= 255 and not i.stub and i.base]
        if cios:
            lines.append("cIOS: " + ", ".join(f"{i.slot}[{i.base}] {i.info}".strip() for i in cios))
        lines.append("")
        lines += [f"{severity_icon(f.severity)} {f.message}" for f in r.findings] or ["✔ Everything needed is installed."]
        s = self.settings
        ct, rg = SC.profile_from_syscheck(r, s.console_type)
        changed = bool(ct and rg and (ct != s.console_type or rg != s.console_region))
        if changed:
            lines.append(f"\nSettings will be updated: console {ct}, region {rg}.")
            s.console_type, s.console_region = ct, rg
        if changed or r.korean_region_changed != s.korean_region_changed:
            s.korean_region_changed = r.korean_region_changed
            self.rapp.save_settings()
        await self.message("SysCheck report", "\n".join(lines))
        fixes = [f.fix for f in r.findings if f.fix in SYSCHECK_FIX_GUIDE]
        if fixes:
            self.show_guide(SYSCHECK_FIX_GUIDE[fixes[0]])

    async def _remove_mod(self, lay: DriveLayout, mods: list[Any]) -> None:
        """Pick one of the mods ReWii installed and take its files off the drive."""
        from ...core import riivolution as RV
        from ..widgets import ChoiceForm
        if not mods:
            await self.message("Nothing to remove", "None of the mods on the drive were installed by ReWii, so "
                               "ReWii doesn't know which files belong to them. Remove those by hand "
                               "(riivolution/ on the drive).")
            return
        res = await self.app.push_screen_wait(ChoiceForm(
            "Remove a mod", "Only the mod's own files are removed; files other mods use stay.",
            [("mod", "Mod", [(m.name, m.name) for m in mods], mods[0].name)]))
        if not res or not res.get("mod"):
            return
        name = res["mod"]
        if not await self.confirm("Remove mod", f"Remove {name} from the drive?\n\nThe copy in your library "
                                  "(Mods) is kept.", danger=True, yes="Remove"):
            return
        removed = await self.bg("Removing the mod", lambda: RV.remove_mod(lay, name))
        if removed is not FAILED:
            await self.message("Mod removed", f"{name}: {len(removed)} file(s) removed from the drive.")

    # ------------------------------------------------------------------ themes / mods
    @work(exclusive=True, group="sys")
    async def action_themes(self) -> None:
        lay = self.need_drive()
        if lay is None:
            return
        from ...core import themes as TH
        staged = await self.bg("Reading the themes folder", lambda: TH.list_staged_themes(lay))
        if staged is FAILED:
            return
        if staged:
            # what is already waiting on the drive for csm-installer
            rows = "\n".join(
                f"  {t.path.name}" + (f"   ({', '.join(x for x in (t.version_label, t.region_name) if x)})"
                                      if t.version_label or t.region_name else "")
                for t in staged)
            ch = await self.choose("Themes", f"Themes on the drive (in themes/), ready for csm-installer:\n{rows}",
                                   a="Add a theme…", b="Done")
            if ch != "a":
                return
        picked = await self.pick("Choose a .csm theme", exts={".csm", ".app"})
        if not picked:
            return
        src, s = picked[0], self.settings
        st = await self.bg("Checking the theme", lambda: TH.stage_theme(
            src, lay, console_type=s.console_type, region=s.console_region))
        if st is FAILED:
            return
        notes = "".join(f"\n• {w}" for w in st.warnings)
        await self.message("Theme ready", f"Copied to {st.dest}.\n\nOn the Wii: Homebrew Channel → csm-installer "
                           "(install it from Homebrew) → pick the theme."
                           + (f"\n\nImportant:{notes}" if notes else ""))

    @work(exclusive=True, group="sys")
    async def action_riivo(self) -> None:
        lay = self.need_drive()
        if lay is None:
            return
        from ...core import riivolution as RV
        mods = await self.bg("Reading the mods on the drive", lambda: RV.list_installed_mods(lay))
        if mods is FAILED:
            return
        if mods:
            rows = "\n".join(f"  {m.name}" + (f"   ({', '.join(m.games)})" if m.games else "")
                             + ("" if m.managed else "   - not installed by ReWii") for m in mods)
            ch = await self.choose("Riivolution mods", f"Mods on the drive:\n{rows}",
                                   a="Add a mod…", b="Remove a mod…")
            if ch is None:
                return
            if ch == "b":
                await self._remove_mod(lay, [m for m in mods if m.managed])
                return
        picked = await self.pick("Choose a Riivolution mod (.zip file, or its folder)", exts={".zip"}, either=True)
        if not picked:
            return
        src, s = picked[0], self.settings
        res = await self.job("Installing mod", lambda p, c: RV.install_mod(src, lay, settings=s),
                             cancellable=False, quiet=True)
        if res is FAILED:
            return
        body = f"{res.name}: {len(res.placed)} file(s)/folder(s) put on the drive."
        if res.warnings:
            body += "\n\nCheck these:\n" + "\n".join(f"• {w}" for w in res.warnings)
        if res.library_copy:
            body += f"\n\nA copy is kept in {res.library_copy}."
        body += ("\n\nInstall the Riivolution app (Homebrew), insert the game disc, start Riivolution "
                 "and enable the mod.")
        await self.message("Mod installed", body, error=bool(res.warnings))
