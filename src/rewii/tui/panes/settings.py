"""Settings."""
from __future__ import annotations

from typing import Any
from collections.abc import Callable

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Label, Select, Static, Switch

from ...core.config import (CONSOLE_TYPES, COVER_LANGS, FORWARDER_STYLES, ONLINE_SERVERS, REGIONS,
                            check_library_path)
from ...core.console import CONSOLE_CHOICE_LABELS
from ..base import FAILED, Pane

CONSOLE_LABELS = CONSOLE_CHOICE_LABELS


class SettingsPane(Pane):
    DEFAULT_CSS = """
    SettingsPane .row { height: auto; margin-bottom: 1; }
    SettingsPane .row Label { width: 30; padding-top: 1; }
    SettingsPane .row Input, SettingsPane .row Select { width: 60; }
    SettingsPane .explain { color: $text-muted; margin: 0 0 1 30; }
    """

    def compose(self) -> ComposeResult:
        s = self.settings
        with VerticalScroll():
            yield Label("Settings", classes="title")
            with Horizontal(classes="row"):
                yield Label("Library folder")
                yield Input(s.library_dir, id="library")
                yield Button("Browse…", id="browse")
            yield Static("Where ReWii keeps your games, channels, saves and backups on this computer.",
                         classes="explain")
            with Horizontal(classes="row"):
                yield Label("Channel launch screen")
                yield Select([("Cover art while loading", "cover"), ("Black screen", "black"),
                              ("Classic loader screen", "classic")],
                             value=s.forwarder_style if s.forwarder_style in FORWARDER_STYLES else "cover",
                             allow_blank=False, id="style")
            yield Static("What you see after opening a game's channel, until the game starts. "
                         "Applies to channels made from now on.", classes="explain")
            with Horizontal(classes="row"):
                yield Label("Shrink Wii games (scrub)")
                yield Switch(s.scrub_games, id="scrub")
            yield Static("Leaves out unused space and the update partition. Saves a lot of room; "
                         "games play the same.", classes="explain")
            with Horizontal(classes="row"):
                yield Label("Online play server")
                yield Select([("Wiimmfi", "wiimmfi"), ("AltWFC", "altwfc"), ("Off", "off")],
                             value=s.online_server if s.online_server in ONLINE_SERVERS else "wiimmfi",
                             allow_blank=False, id="online")
            yield Static("Written to USB Loader GX when you press Save (if a drive is selected), so online "
                         "games (Mario Kart Wii…) use a replacement for Nintendo WFC. For WiiLink WFC, install "
                         "the 'WiiLink WFC Patcher' app from Homebrew instead.", classes="explain")
            with Horizontal(classes="row"):
                yield Label("Cover language")
                yield Select([(x, x) for x in COVER_LANGS],
                             value=s.cover_lang if s.cover_lang in COVER_LANGS else "EN",
                             allow_blank=False, id="lang")
            yield Label("Console (only for system features)", classes="title")
            yield Static("Games, homebrew and saves don't care which Wii you have. This is only used to "
                         "pick the right system files (cIOS, updates, themes) - wrong ones can brick a Wii.",
                         classes="hint")
            with Horizontal(classes="row"):
                yield Label("Console type")
                yield Select([(CONSOLE_LABELS[c], c) for c in CONSOLE_TYPES],
                             value=s.console_type if s.console_type in CONSOLE_TYPES else Select.NULL,
                             prompt="Not set", id="ctype")
            with Horizontal(classes="row"):
                yield Label("Region")
                yield Select([(r, r) for r in REGIONS],
                             value=s.console_region if s.console_region in REGIONS else Select.NULL,
                             prompt="Not set", id="region")
            with Horizontal(classes="row"):
                yield Label("Korean Wii, region changed")
                yield Switch(s.korean_region_changed, id="korean")
            yield Static("Turned on by a SysCheck report when a Wii made for Korea was changed to another "
                         "region. While it is on, ReWii refuses Wii Menu updates and Wii Menu WADs (they "
                         "would brick such a console: Error 003). Only turn it off if that is wrong.",
                         classes="explain")
            with Horizontal(classes="toolbar"):
                yield Button("Save", id="save", variant="primary")
                yield Button("USB Loader GX settings on the drive…", id="gx")

    def button_actions(self) -> dict[str, Callable[[], Any]]:
        return {"browse": self._browse, "gx": self._gx_edit, "save": self._save}

    def focus_main(self) -> None:
        self.query_one("#library", Input).focus()

    def reload(self) -> None:
        """Show the current settings - they can change elsewhere (SysCheck sets the console,
        'Send to Wii' remembers the address), and Save must never write stale values back."""
        s = self.settings
        self.query_one("#library", Input).value = s.library_dir
        self.query_one("#scrub", Switch).value = bool(s.scrub_games)
        self.query_one("#korean", Switch).value = bool(s.korean_region_changed)
        fixed: list[tuple[str, str, tuple[str, ...]]] = [
            ("#style", s.forwarder_style, FORWARDER_STYLES), ("#online", s.online_server, ONLINE_SERVERS),
            ("#lang", s.cover_lang, COVER_LANGS)]
        for wid, value, allowed in fixed:
            if value in allowed:
                self.query_one(wid, Select).value = value
        optional: list[tuple[str, str, tuple[str, ...]]] = [
            ("#ctype", s.console_type, CONSOLE_TYPES), ("#region", s.console_region, REGIONS)]
        for wid, value, allowed in optional:
            sel = self.query_one(wid, Select)
            if value in allowed:
                sel.value = value
            else:
                sel.clear()

    @work(exclusive=True, group="settings")
    async def _browse(self) -> None:
        p = await self.pick("Library folder", folder=True, start=self.settings.library)
        if p:
            self.query_one("#library", Input).value = str(p[0])

    @work(exclusive=True, group="settings")
    async def _gx_edit(self) -> None:
        lay = self.need_drive()
        if lay is None:
            return
        from ...core import online
        from ..widgets import ChoiceForm
        cur = await self.bg("Reading USB Loader GX's settings", lambda: online.read_gx_config(lay))
        if cur is FAILED:
            return
        fields = []
        for key, opt in online.GX_OPTIONS.items():
            if not opt.choices or key == "PrivateServer":
                continue
            fields.append((key, opt.label, [(t, v) for v, t in opt.choices.items()], cur.get(key, opt.default)))
        res = await self.app.push_screen_wait(ChoiceForm(
            "USB Loader GX settings", "Saved into apps/usbloader_gx/GXGlobal.cfg on the drive. "
            "Everything else can be changed in USB Loader GX itself.", fields))
        if res:
            if await self.bg("Saving USB Loader GX's settings",
                             lambda: online.write_gx_config(lay, res)) is not FAILED:
                self.app.notify("USB Loader GX settings saved")

    @staticmethod
    def _choice(sel: Select, allowed: tuple[str, ...], blank_ok: bool = False) -> str | None:
        """The dropdown's value as a setting, or None when it isn't a valid one."""
        v = sel.value
        if isinstance(v, str) and v in allowed:
            return v
        return "" if blank_ok else None

    def _save(self) -> None:
        s = self.settings
        # read and check everything first; settings are only touched once it is all valid
        try:
            lib = check_library_path(self.query_one("#library", Input).value)
        except ValueError as e:
            self.app.notify(str(e), severity="error")
            return
        style = self._choice(self.query_one("#style", Select), FORWARDER_STYLES)
        online_server = self._choice(self.query_one("#online", Select), ONLINE_SERVERS)
        lang = self._choice(self.query_one("#lang", Select), COVER_LANGS)
        ctype = self._choice(self.query_one("#ctype", Select), CONSOLE_TYPES, blank_ok=True)
        region = self._choice(self.query_one("#region", Select), REGIONS, blank_ok=True)
        if style is None or online_server is None or lang is None or ctype is None or region is None:
            self.app.notify("Pick a value for every setting first", severity="error")
            return
        old_lib = s.library_dir
        s.library_dir = str(lib)
        try:
            s.ensure_library()
        except OSError as e:
            s.library_dir = old_lib
            self.app.notify(f"Can't use that library folder: {e}", severity="error")
            return
        s.forwarder_style, s.online_server, s.cover_lang = style, online_server, lang
        s.scrub_games = self.query_one("#scrub", Switch).value
        s.console_type, s.console_region = ctype, region
        s.korean_region_changed = self.query_one("#korean", Switch).value
        if not self.rapp.save_settings():
            return
        if str(lib) != old_lib:
            self.app.notify(f"The library is now {lib}. Files in the old folder ({old_lib}) were not moved.",
                            timeout=10)
        lay = self.layout_
        if lay and lay.gx_config.parent.exists():
            try:
                from ...core.online import set_loader_private_server
                set_loader_private_server(lay, s.online_server)
            except Exception as e:  # noqa: BLE001 - reported
                self.app.notify(f"Couldn't update USB Loader GX settings: {e}", severity="warning")
        self.app.notify("Settings saved")
