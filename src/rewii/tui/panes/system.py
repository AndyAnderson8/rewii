"""System & Guides: step-by-step guides, SysCheck, and system files (cIOS, 4.3 update, themes, mods)."""
from __future__ import annotations

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Label, ListItem, ListView, Markdown, Static

from ...core import guides as GD
from ..base import Pane

# guide.prepare -> OSC slugs to install
PREPARE_APPS = {
    "nanddumper": ["nanddumper_ios"],
    "priiloader": ["priiloader", "LoadPriiloader"],
    "syscheck": ["SysCheckME"],
    "cleanrip": ["CleanRip"],
}
SYSCHECK_FIX_GUIDE = {"install_priiloader": "priiloader", "install_d2x_cios": "cios",
                      "update_43": "recovery", "restore_ios": "recovery"}


class SystemPane(Pane):
    TITLE = "System & Guides"
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

    @on(Button.Pressed)
    def _b(self, ev: Button.Pressed) -> None:
        {"prepare": self.action_prepare, "syscheck": self.action_syscheck, "cios": self.action_cios,
         "update": self.action_update, "themes": self.action_themes, "riivo": self.action_riivo}[ev.button.id]()

    # ------------------------------------------------------------------ prepare
    async def _install_slugs(self, slugs: list[str]) -> bool:
        from ...core import osc
        lay = self.layout_
        plat = self.settings.console_type

        def run(progress, cancel):
            return osc.install_starter_kit(lay, plat, slugs, progress, cancel)

        rep = await self.job("Installing apps", run)
        if rep is None:
            return False
        if rep.failed or rep.not_found:
            await self.message("Some apps couldn't be installed",
                               "\n".join([f"{k}: {v}" for k, v in dict(rep.failed).items()] +
                                         [f"{k}: not on the Open Shop Channel" for k in rep.not_found]), error=True)
        return True

    @work(exclusive=True, group="sys")
    async def action_prepare(self) -> None:
        g = self.current
        if not g.prepare or not self.need_drive():
            return
        if g.prepare == "cios":
            await self._cios()
            return
        if await self._install_slugs(PREPARE_APPS[g.prepare]):
            await self.message("Ready", f"The files for “{g.title}” are on the drive. Follow the steps on the Wii.")

    async def _cios(self) -> None:
        from ...core import nus
        from ...core.wad import Refused
        s, lay = self.settings, self.layout_
        if s.console_type == "mini":
            await self.message("Wii mini", "The Wii mini needs the special d2xl cIOS installer, which isn't on "
                               "the Open Shop Channel. Get it from https://wii.hacks.guide/cios (Wii mini tab).")
            return
        try:
            entries = nus.cios_entries(s)
        except Refused as e:
            await self.message("Console needed", f"{e}\n\nSet your console type and region in Settings.", error=True)
            return
        if not await self.confirm("cIOS files", "Download these from Nintendo's servers to the drive:\n  " +
                                  "\n  ".join(getattr(e, "filename", str(e)) for e in entries) +
                                  "\n\nand install the d2x cIOS installer app?"):
            return
        res = await self.job("Downloading cIOS base files", lambda p, c: nus.prepare_cios_files(lay, s, p, c))
        if res is None:
            return
        slug = "d2x-cios-installer-vwii" if s.console_type == "vwii" else "d2x-cios-installer"
        if await self._install_slugs([slug]):
            self.show_guide("cios")
            await self.message("Ready", "Now follow the cIOS guide on the Wii.")

    @work(exclusive=True, group="sys")
    async def action_cios(self) -> None:
        if self.need_drive():
            await self._cios()

    @work(exclusive=True, group="sys")
    async def action_update(self) -> None:
        if not self.need_drive():
            return
        from ...core import nus
        from ...core.wad import Refused
        s, lay = self.settings, self.layout_
        try:
            entries = nus.update_43_entries(s)
        except Refused as e:
            await self.message("Not available", str(e), error=True)
            return
        if not await self.confirm(
                "Update to System Menu 4.3",
                "Only needed if your Wii is on an older System Menu (SysCheck tells you).\n\nDownloads:\n  " +
                "\n  ".join(getattr(e, "filename", str(e)) for e in entries) +
                "\n\ninto the drive's wad/ folder. Install them with YAWMME: the two IOS first, then the System "
                "Menu. Have Priiloader and a NAND backup first!"):
            return
        await self.job("Downloading update files", lambda p, c: nus.prepare_update_43(lay, s, p, c))

    # ------------------------------------------------------------------ syscheck
    @work(exclusive=True, group="sys")
    async def action_syscheck(self) -> None:
        from ...core import syscheck as SC
        path = SC.find_syscheck(self.layout_.root) if self.layout_ else None
        if path is None:
            picked = await self.pick("Find syscheck.csv", exts={".csv", ".txt"})
            if not picked:
                return
            path = picked[0]
        try:
            r = SC.read_syscheck(path)
        except Exception as e:
            await self.message("Couldn't read the report", str(e), error=True)
            return
        lines = [f"System Menu: {r.sysmenu or '?'}   Region: {r.region or '?'}   Console: {r.console_type or '?'}",
                 f"Homebrew Channel: {'.'.join(map(str, r.hbc_version)) if r.hbc_version else 'not found'}   "
                 f"Priiloader: {'yes' if r.priiloader else 'no'}"]
        cios = [i for slot, i in sorted(r.ios.items()) if 200 <= slot <= 255 and not i.stub and i.base]
        if cios:
            lines.append("cIOS: " + ", ".join(f"{i.slot}[{i.base}] {i.info}".strip() for i in cios))
        lines.append("")
        lines += [f"{'✖' if f.severity == 'error' else '!' if f.severity == 'warn' else 'i'} {f.message}"
                  for f in r.findings] or ["✔ Everything needed is installed."]
        ct, rg = SC.profile_from_syscheck(r)
        s = self.settings
        if ct and rg and (ct != s.console_type or rg != s.console_region):
            lines.append(f"\nSettings will be updated: console {ct}, region {rg}.")
            s.console_type, s.console_region = ct, rg
            s.save()
        await self.message("SysCheck report", "\n".join(lines))
        fixes = [f.fix for f in r.findings if f.fix in SYSCHECK_FIX_GUIDE]
        if fixes:
            self.show_guide(SYSCHECK_FIX_GUIDE[fixes[0]])

    # ------------------------------------------------------------------ themes / mods
    @work(exclusive=True, group="sys")
    async def action_themes(self) -> None:
        if not self.need_drive():
            return
        from ...core import themes as TH
        picked = await self.pick("Choose a .csm theme", exts={".csm"})
        if not picked:
            return
        try:
            st = TH.stage_theme(picked[0], self.layout_.root, console_type=self.settings.console_type,
                                 region=self.settings.console_region)
            out = getattr(st, 'path', st)
        except Exception as e:
            await self.message("Theme not installed", str(e), error=True)
            return
        await self.message("Theme ready", f"Copied to {out}.\n\nOn the Wii: Homebrew Channel → csm-installer "
                           "(install it from Homebrew) → pick the theme. Make sure Priiloader is installed first: "
                           "a bad theme can stop the Wii Menu from starting.")

    @work(exclusive=True, group="sys")
    async def action_riivo(self) -> None:
        if not self.need_drive():
            return
        from ...core import riivolution as RV
        picked = await self.pick("Choose a Riivolution mod (.zip or folder)", exts={".zip"})
        if not picked:
            return
        lay, s = self.layout_, self.settings
        res = await self.job("Installing mod", lambda p, c: RV.install_mod(picked[0], lay.root, settings=s))
        if res is not None:
            await self.message("Mod installed", f"{res}\n\nInstall the Riivolution app (Homebrew), insert the game "
                               "disc, start Riivolution and enable the mod.")
