"""Base class for the app's sections ("panes") and shared async helpers."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from textual.containers import Vertical

from ..core.progress import Cancelled
from .widgets import Confirm, Job, JobFailed, Message, PickPath, Prompt

if TYPE_CHECKING:
    from .app import ReWiiApp


class Pane(Vertical):
    """A section of the app. Subclasses implement ``reload()``."""

    TITLE = ""
    DEFAULT_CSS = """
    Pane { padding: 0 1; }
    Pane .title { text-style: bold; color: $accent; margin: 0 0 1 0; }
    Pane .hint { color: $text-muted; margin-bottom: 1; }
    Pane .toolbar { height: auto; margin-bottom: 1; }
    Pane .toolbar Button { margin-right: 1; min-width: 8; height: 1; border: none; line-pad: 1; }
    Pane .toolbar Button.-primary, Pane .toolbar Button.-warning, Pane .toolbar Button.-error,
    Pane .toolbar Button:focus, Pane .toolbar Button:hover { border: none; height: 1; }
    Pane .toolbar Select { width: 30; margin-right: 1; }
    Pane .toolbar Input { height: 1; border: none; width: 30; }
    Pane DataTable { height: 1fr; }
    """

    @property
    def rapp(self) -> "ReWiiApp":
        return self.app  # type: ignore[return-value]

    @property
    def settings(self):
        return self.rapp.settings

    @property
    def layout_(self):
        return self.rapp.drive_layout

    def reload(self) -> None:
        """Refresh data shown in the pane (called when shown / drive changes)."""

    def need_drive(self) -> bool:
        if self.rapp.drive_layout is None:
            self.app.notify("Choose your Wii's USB drive first (Drive section, or press d)",
                            severity="warning")
            return False
        return True

    # ---- async dialog helpers; call from @work(exclusive=...) async methods
    async def confirm(self, title: str, body: str, **kw) -> bool:
        return bool(await self.app.push_screen_wait(Confirm(title, body, **kw)))

    async def message(self, title: str, body: str, error: bool = False) -> None:
        await self.app.push_screen_wait(Message(title, body, error))

    async def prompt(self, title: str, label: str, value: str = "") -> Optional[str]:
        return await self.app.push_screen_wait(Prompt(title, label, value))

    async def pick(self, title: str, **kw) -> Optional[list[Path]]:
        return await self.app.push_screen_wait(PickPath(title, **kw))

    async def job(self, title: str, fn: Callable, cancellable: bool = True,
                  quiet: bool = False) -> Any:
        """Run fn(progress, cancel) with a progress dialog. Returns result or None on failure."""
        res = await self.app.push_screen_wait(Job(title, fn, cancellable))
        if isinstance(res, JobFailed):
            self.rapp.log_error(title, res)
            await self.message(f"{title} failed", str(res), error=True)
            return None
        if isinstance(res, Cancelled):
            self.app.notify(f"{title}: cancelled", severity="warning")
            return None
        if not quiet:
            self.app.notify(f"{title}: done")
        return res
