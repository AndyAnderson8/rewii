"""Settings."""
from __future__ import annotations

from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Label, Select, Static, Switch

from ...core.config import CONSOLE_TYPES, REGIONS
from ..base import Pane

CONSOLE_LABELS = {"wii": "Wii (original)", "vwii": "Wii U (vWii)", "mini": "Wii mini",
                  "family": "Wii Family Edition"}


class SettingsPane(Pane):
    TITLE = "Settings"
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
                              ("Classic loader screen", "classic")], value=s.forwarder_style,
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
                yield Select([("WiiLink WFC", "wiilink"), ("Wiimmfi", "wiimmfi"), ("Off", "")],
                             value=s.online_server, allow_blank=False, id="online")
            yield Static("Set in USB Loader GX so online games (Mario Kart Wii…) connect to a "
                         "replacement for Nintendo WFC.", classes="explain")
            with Horizontal(classes="row"):
                yield Label("Cover language")
                yield Select([(x, x) for x in ("EN", "US", "FR", "DE", "ES", "IT", "NL", "JA", "KO", "ZH")],
                             value=s.cover_lang if s.cover_lang else "EN", allow_blank=False, id="lang")
            yield Label("Console (only for system features)", classes="title")
            yield Static("Games, homebrew and saves don't care which Wii you have. This is only used to "
                         "pick the right system files (cIOS, updates, themes) - wrong ones can brick a Wii.",
                         classes="hint")
            with Horizontal(classes="row"):
                yield Label("Console type")
                yield Select([(CONSOLE_LABELS[c], c) for c in CONSOLE_TYPES],
                             value=s.console_type or Select.NULL, prompt="Not set", id="ctype")
            with Horizontal(classes="row"):
                yield Label("Region")
                yield Select([(r, r) for r in REGIONS], value=s.console_region or Select.NULL,
                             prompt="Not set", id="region")
            with Horizontal(classes="toolbar"):
                yield Button("Save", id="save", variant="primary")

    @work(exclusive=True, group="settings")
    async def _browse(self) -> None:
        p = await self.pick("Library folder", folder=True, start=self.settings.library)
        if p:
            self.query_one("#library", Input).value = str(p[0])

    @on(Button.Pressed, "#browse")
    def _b(self) -> None:
        self._browse()

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        s = self.settings
        lib = Path(self.query_one("#library", Input).value.strip()).expanduser()
        s.library_dir = str(lib)
        s.forwarder_style = self.query_one("#style", Select).value  # type: ignore[assignment]
        s.scrub_games = self.query_one("#scrub", Switch).value
        s.online_server = self.query_one("#online", Select).value  # type: ignore[assignment]
        s.cover_lang = self.query_one("#lang", Select).value  # type: ignore[assignment]
        ct = self.query_one("#ctype", Select).value
        rg = self.query_one("#region", Select).value
        s.console_type = "" if ct is Select.NULL else str(ct)
        s.console_region = "" if rg is Select.NULL else str(rg)
        try:
            s.ensure_library()
        except OSError as e:
            self.app.notify(f"Can't use that library folder: {e}", severity="error")
            return
        s.save()
        self.app.notify("Settings saved")
