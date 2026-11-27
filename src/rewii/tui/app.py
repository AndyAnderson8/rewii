"""ReWii terminal app."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import ContentSwitcher, Footer, Header, ListItem, ListView, Label

from .. import __version__
from ..core.config import Settings, config_dir
from ..core.layout import DriveLayout

if TYPE_CHECKING:
    from ..core.drives import Drive
    from ..core.gametdb import GameInfo, TitleDB
    from .base import Pane

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


def _make_pane(key: str) -> Pane:
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
        self.drive_info: Optional[Drive] = None
        self._titles: Optional[TitleDB] = None
        self.startup_problems: list[str] = []
        start = drive or (Path(self.settings.last_drive) if self.settings.last_drive else None)
        try:
            if start and start.is_dir():
                self.drive_layout = DriveLayout(start)
            elif drive is not None:
                self.startup_problems.append(f"The drive {drive} isn't there - choose a drive in the Drive section.")
        except OSError:
            pass
        try:
            self.settings.ensure_library()
        except OSError as e:
            # keep running so the user can pick another folder in Settings
            self.startup_problems.append(
                f"The library folder {self.settings.library} can't be used ({e.strerror or e}). "
                "Reconnect its disk, or choose another folder in Settings.")

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
        self.pane("home").reload()
        for msg in self.startup_problems:
            self.notify(msg, severity="error", timeout=30)
        if not self.settings.first_run_done:
            self.call_after_refresh(self.pane("home").first_run)  # type: ignore[attr-defined]

    def pane(self, key: str) -> Pane:
        from .base import Pane
        return self.query_one(f"#{key}", Pane)

    def _update_subtitle(self) -> None:
        d = self.drive_layout
        self.sub_title = f"v{__version__} · drive: {d.root}" if d else f"v{__version__} · no drive selected"

    @on(ListView.Selected, "#sidebar")
    def _nav(self, ev: ListView.Selected) -> None:
        key = (ev.item.id or "nav-home")[4:]
        self.action_section(key)

    def action_section(self, key: str) -> None:
        keys = [k for k, _ in SECTIONS]
        if key not in keys:
            return
        sw = self.query_one(ContentSwitcher)
        sw.current = key
        pane = self.pane(key)
        pane.reload()
        lv = self.query_one("#sidebar", ListView)
        idx = keys.index(key)
        if lv.index != idx:
            lv.index = idx
        # move the keyboard into the section so its shortcuts (shown in the footer) work
        self.call_after_refresh(pane.focus_main)

    def action_reload(self) -> None:
        sw = self.query_one(ContentSwitcher)
        if sw.current:
            pane = self.pane(sw.current)
            getattr(pane, "refresh_data", pane.reload)()

    # ------------------------------------------------------------------ state
    def set_drive(self, root: Path | None, info: Optional[Drive] = None) -> None:
        self.drive_layout = DriveLayout(root) if root else None
        self.drive_info = info
        self.settings.last_drive = str(root) if root else ""
        self.save_settings()
        self._update_subtitle()
        self.notify(f"Using drive {root}" if root else "No drive selected")

    def save_settings(self) -> bool:
        """Save settings; a read-only config folder is reported, not fatal."""
        try:
            self.settings.save()
            return True
        except OSError as e:
            self.notify(f"Couldn't save settings: {e}", severity="error")
            return False

    @work(thread=True, exclusive=True, group="titles")
    def load_titles(self) -> None:
        db: Optional[TitleDB]
        try:
            from ..core.gametdb import TitleDB
            db = TitleDB.load(self.settings)
        except Exception as e:  # offline and no cache: titles come from the discs
            log.info("title database unavailable: %s", e)
            db = None
        self._titles = db
        if db is not None:
            try:
                self.call_from_thread(self._titles_ready)
            except RuntimeError:
                pass

    def _titles_ready(self) -> None:
        """Lists read before the title database arrived show raw disc titles: read them again."""
        try:
            self.pane("games").titles_ready()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - pane not mounted yet
            pass

    @work(thread=True, exclusive=True, group="update")
    def check_update(self) -> None:
        try:
            from ..core.sysfiles import check_for_update
            info = check_for_update(__version__)
        except Exception:
            return
        if info and info.newer:
            try:
                self.call_from_thread(self.notify, f"ReWii {info.latest} is available: {info.url}", timeout=15)
            except RuntimeError:
                pass

    def title_lookup(self, game_id: str) -> Optional[str]:
        db = self._titles
        if db is None:
            return None
        try:
            g = db.get(game_id)
            return g.title if g else None
        except Exception:
            return None

    def game_info(self, game_id: str) -> Optional[GameInfo]:
        """GameTDB entry (publisher, year, synopsis…) or None."""
        db = self._titles
        try:
            return db.get(game_id) if db is not None else None
        except Exception:
            return None

    def log_error(self, what: str, failure: object) -> None:
        log.error("%s failed: %s\n%s", what, failure, getattr(failure, "tb", ""))


def setup_logging() -> Path:
    p = config_dir() / "rewii.log"
    logging.basicConfig(filename=p, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return p


def run(drive: Path | None = None) -> None:
    setup_logging()
    ReWiiApp(drive=drive).run()
