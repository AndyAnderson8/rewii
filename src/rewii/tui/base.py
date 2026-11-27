"""Base class for the app's sections ("panes") and shared async helpers."""
from __future__ import annotations

import asyncio
import logging
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from collections.abc import Callable, Sequence

from textual import on
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Button, DataTable

from ..core.config import Settings
from ..core.fileops import explain_os_error
from ..core.findings import Severity
from ..core.layout import DriveLayout
from ..core.progress import Cancelled
from .widgets import Choice, Confirm, Job, JobFailed, Message, PickPath, Prompt, cursor_key

if TYPE_CHECKING:
    from .app import ReWiiApp

log = logging.getLogger("rewii")


#: The one severity -> icon mapping (drive health, SysCheck report, WAD risks).
SEVERITY_ICON = {Severity.ERROR: "✖", Severity.WARNING: "!", Severity.INFO: "i"}


def severity_icon(severity: object) -> str:
    try:
        return SEVERITY_ICON[Severity(severity)]        # type: ignore[arg-type]
    except ValueError:
        return "?"


class _Failed:
    """What :meth:`Pane.job` / :meth:`Pane.bg` return when the work failed or was
    cancelled (the user has already been told). Falsy, and distinct from a
    ``None`` result, which is a normal return value."""

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "FAILED"


FAILED: Any = _Failed()


def titles(names: Sequence[str], limit: int = 25) -> str:
    """An indented list of names for a confirmation dialog."""
    shown = [f"  {n}" for n in names[:limit]]
    if len(names) > limit:
        shown.append(f"  … and {len(names) - limit} more")
    return "\n".join(shown)


class Pane(Vertical):
    """A section of the app. Subclasses implement ``reload()``."""

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
    def rapp(self) -> ReWiiApp:
        return self.app  # type: ignore[return-value]

    @property
    def settings(self) -> Settings:
        return self.rapp.settings

    @property
    def layout_(self) -> Optional[DriveLayout]:
        return self.rapp.drive_layout

    def reload(self) -> None:
        """Refresh data shown in the pane (called when shown / drive changes)."""

    def focus_main(self) -> None:
        """Put the keyboard in the pane (its first table, else its first control), so
        the pane's shortcuts work right after switching to it."""
        for table in self.query(DataTable):
            table.focus()
            return
        for w in self.query("*"):
            if w.focusable and not w.disabled:
                w.focus()
                return

    def need_drive(self) -> Optional[DriveLayout]:
        """The drive in use, or None after telling the user to choose one."""
        lay = self.rapp.drive_layout
        if lay is None:
            self.app.notify("Choose your Wii's USB drive first (Drive section, or press d)",
                            severity="warning")
            return None
        if not lay.connected():
            # never carry on: writing would re-create the folder on this computer's own disk
            self.app.notify(f"The drive {lay.root} isn't connected. Plug it in (and wait until the "
                            "computer shows it), or choose another drive in the Drive section.",
                            severity="error", timeout=10)
            return None
        return lay

    @property
    def live_layout(self) -> Optional[DriveLayout]:
        """The drive in use if it is connected right now, else None (without a message)."""
        lay = self.rapp.drive_layout
        return lay if lay is not None and lay.connected() else None

    def explain(self, exc: object) -> str:
        """Plain words for an error shown to the user (see ``fileops.explain_os_error``)."""
        e = getattr(exc, "exc", exc)             # a JobFailed wraps the real exception
        lay = self.rapp.drive_layout
        if isinstance(e, BaseException):
            return explain_os_error(e, lay.root if lay is not None else None)
        return str(exc)

    # ---- buttons: one safe dispatcher for every pane
    def button_actions(self) -> dict[str, Callable[[], Any]]:
        """Button id -> what it does. Ids not listed are ignored."""
        return {}

    @on(Button.Pressed)
    def _button_pressed(self, ev: Button.Pressed) -> None:
        act = self.button_actions().get(ev.button.id or "")
        if act is not None:
            ev.stop()
            act()

    # ---- tables
    def cursor_index(self, selector: str) -> Optional[int]:
        """Index stored as the row key of the highlighted row of an index-keyed table."""
        try:
            key = cursor_key(self.query_one(selector, DataTable))
        except NoMatches:
            return None
        try:
            return int(key) if key is not None else None
        except ValueError:
            return None

    # ---- background scans (thread workers)
    def from_thread(self, fn: Callable[..., Any], *args: Any) -> None:
        """``call_from_thread`` that doesn't blow up when the app is closing.

        A scan can finish after the screen has started to come down; its result then
        has nowhere to go (the widgets are gone), which must not count as a crash."""
        def run() -> None:
            if not self.is_attached:
                return
            try:
                fn(*args)
            except NoMatches:
                if self.is_attached and self.app.is_running:
                    raise

        try:
            self.app.call_from_thread(run)
        except (RuntimeError, NoMatches):
            pass

    def scan_failed(self, what: str, exc: BaseException) -> None:
        """Report a failed background scan (call from the scan thread). The app keeps running."""
        log.error("%s failed: %s\n%s", what, exc, traceback.format_exc())
        self.from_thread(self._notify_error, f"{what} failed: {exc or type(exc).__name__}")

    def _notify_error(self, text: str) -> None:
        self.app.notify(text, severity="error", timeout=10)

    # ---- async dialog helpers; call from @work(exclusive=...) async methods
    async def confirm(self, title: str, body: str, **kw: Any) -> bool:
        """True only when the user pressed the "yes" button (Escape and "no" both cancel)."""
        return await self.app.push_screen_wait(Confirm(title, body, **kw)) is True

    async def choose(self, title: str, body: str, a: str, b: str) -> Optional[str]:
        """Either/or question: "a", "b", or None when the user cancelled."""
        return await self.app.push_screen_wait(Choice(title, body, a, b))

    async def message(self, title: str, body: str, error: bool = False) -> None:
        await self.app.push_screen_wait(Message(title, body, error))

    async def prompt(self, title: str, label: str, value: str = "") -> Optional[str]:
        return await self.app.push_screen_wait(Prompt(title, label, value))

    async def pick(self, title: str, **kw: Any) -> Optional[list[Path]]:
        return await self.app.push_screen_wait(PickPath(title, **kw))

    async def job(self, title: str, fn: Callable[[Any, Any], Any], cancellable: bool = True,
                  quiet: bool = False) -> Any:
        """Run ``fn(progress, cancel)`` in a thread behind a progress dialog.

        Returns its result (which may be None), or :data:`FAILED` if it raised or
        was cancelled - the user has been told by then.
        """
        res = await self.app.push_screen_wait(Job(title, fn, cancellable))
        if isinstance(res, JobFailed):
            self.rapp.log_error(title, res)
            await self.message(f"{title} failed", self.explain(res), error=True)
            return FAILED
        if isinstance(res, Cancelled):
            self.app.notify(f"{title}: cancelled", severity="warning")
            return FAILED
        if not quiet:
            self.app.notify(f"{title}: done")
        return res

    async def bg(self, what: str, fn: Callable[[], Any]) -> Any:
        """Run quick blocking work off the UI thread without a dialog.

        Returns its result, or :data:`FAILED` after showing the error.
        """
        try:
            return await asyncio.to_thread(fn)
        except Exception as e:  # noqa: BLE001 - shown to the user
            log.error("%s failed: %s\n%s", what, e, traceback.format_exc())
            await self.message(f"{what} failed", self.explain(e), error=True)
            return FAILED


def kit_lines(rep: Any) -> tuple[list[str], bool]:
    """Plain-language lines for an ``osc.StarterKitReport`` and whether anything went wrong."""
    from ..core.osc import kit_lines as core_kit_lines
    return core_kit_lines(rep)
