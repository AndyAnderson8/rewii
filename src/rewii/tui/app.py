"""ReWii terminal app."""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import ContentSwitcher, Footer, Header, ListItem, ListView, Label

from .. import __version__
from ..core.config import Settings, config_dir
from ..core.layout import DriveLayout

log = logging.getLogger("rewii")

SECTIONS = [
    ("home", "Home"),
    ("drive", "Drive"),
    ("games", "Games"),
    ("channels", "Channels & WADs"),
    ("homebrew", "Homebrew"),
    ("saves", "Saves"),
    ("backups", "Backups"),
    ("system", "System & Guides"),
    ("settings", "Settings"),
]


def _make_pane(key: str):
    from .panes import backups, channels, drive, games, home, homebrew, saves, settings, system
    return {
        "home": home.HomePane, "drive": drive.DrivePane, "games": games.GamesPane,
        "channels": channels.ChannelsPane, "homebrew": homebrew.HomebrewPane,
        "saves": saves.SavesPane, "backups": backups.BackupsPane, "system": system.SystemPane,
        "settings": settings.SettingsPane,
    }[key](id=key)


class ReWiiApp(App):
    TITLE = "ReWii"
    SUB_TITLE = "all-in-one Wii toolkit"
    CSS = """
    #sidebar { width: 22; border-right: solid $primary-background; }
    #sidebar ListItem { padding: 0 1; }
    #content { width: 1fr; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("d", "section('drive')", "Drive", show=False),
        Binding("g", "section('games')", "Games", show=False),
        Binding("f5", "reload", "Refresh"),
        Binding("question_mark", "section('system')", "Help", show=False),
    ]

    def __init__(self, settings: Settings | None = None, drive: Path | None = None) -> None:
        super().__init__()
        self.settings = settings or Settings.load()
        self.drive_layout: Optional[DriveLayout] = None
        self.drive_info = None          # rewii.core.drives.Drive when known
        self._titles = None
        self._titles_lock = threading.Lock()
        start = drive or (Path(self.settings.last_drive) if self.settings.last_drive else None)
        if start and start.exists():
            self.drive_layout = DriveLayout(start)
        self.settings.ensure_library()

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            yield ListView(*[ListItem(Label(t), id=f"nav-{k}") for k, t in SECTIONS], id="sidebar")
            with ContentSwitcher(initial="home", id="content"):
                for k, _ in SECTIONS:
                    yield _make_pane(k)
        yield Footer()

    def on_mount(self) -> None:
        self._update_subtitle()
        self.load_titles()
        self.check_update()
        self.query_one("#home").reload()
        if not self.settings.first_run_done:
            self.call_after_refresh(self.query_one("#home").first_run)  # type: ignore[attr-defined]

    def _update_subtitle(self) -> None:
        d = self.drive_layout
        self.sub_title = f"v{__version__} · drive: {d.root}" if d else f"v{__version__} · no drive selected"

    @on(ListView.Selected, "#sidebar")
    def _nav(self, ev: ListView.Selected) -> None:
        key = (ev.item.id or "nav-home")[4:]
        self.action_section(key)

    def action_section(self, key: str) -> None:
        sw = self.query_one(ContentSwitcher)
        sw.current = key
        pane = self.query_one(f"#{key}")
        pane.reload()  # type: ignore[attr-defined]
        lv = self.query_one("#sidebar", ListView)
        idx = [k for k, _ in SECTIONS].index(key)
        if lv.index != idx:
            lv.index = idx

    def action_reload(self) -> None:
        sw = self.query_one(ContentSwitcher)
        if sw.current:
            self.query_one(f"#{sw.current}").reload()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ state
    def set_drive(self, root: Path | None, info=None) -> None:
        self.drive_layout = DriveLayout(root) if root else None
        self.drive_info = info
        self.settings.last_drive = str(root) if root else ""
        self.settings.save()
        self._update_subtitle()
        self.notify(f"Using drive {root}" if root else "No drive selected")

    @work(thread=True, exclusive=True, group="titles")
    def load_titles(self) -> None:
        try:
            from ..core.gametdb import TitleDB
            db = TitleDB.load(self.settings)
        except Exception as e:  # offline and no cache: titles come from the discs
            log.info("title database unavailable: %s", e)
            db = None
        with self._titles_lock:
            self._titles = db

    @work(thread=True, exclusive=True, group="update")
    def check_update(self) -> None:
        try:
            from ..core.sysfiles import check_for_update
            info = check_for_update(__version__)
        except Exception:
            return
        if info and info.newer:
            self.call_from_thread(self.notify, f"ReWii {info.latest} is available: {info.url}", timeout=15)

    def title_lookup(self, game_id: str) -> Optional[str]:
        db = self._titles
        if db is None:
            return None
        try:
            g = db.get(game_id)
            return g.title if g else None
        except Exception:
            return None

    def game_info(self, game_id: str):
        """GameTDB entry (publisher, year, synopsis…) or None."""
        db = self._titles
        try:
            return db.get(game_id) if db is not None else None
        except Exception:
            return None

    def log_error(self, what: str, failure) -> None:
        log.error("%s failed: %s\n%s", what, failure, getattr(failure, "tb", ""))


def setup_logging() -> Path:
    p = config_dir() / "rewii.log"
    logging.basicConfig(filename=p, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return p


def run(drive: Path | None = None) -> None:
    setup_logging()
    ReWiiApp(drive=drive).run()
