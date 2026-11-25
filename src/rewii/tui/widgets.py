"""Reusable dialogs: confirm, message, job progress (with cancel), file/folder picker, text prompt."""
from __future__ import annotations

import os
import string
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Input, Label, ProgressBar, Static

from ..core.progress import Cancelled, CancelToken, ProgressEvent


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return str(n)


class Confirm(ModalScreen[bool]):
    """Yes/No dialog. If ``type_to_confirm`` is set the user must type it first."""

    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical { width: 72; max-width: 95%; height: auto; border: thick $warning; background: $surface; padding: 1 2; }
    Confirm.danger > Vertical { border: thick $error; }
    Confirm #body { margin-bottom: 1; }
    Confirm Horizontal { height: auto; align-horizontal: right; }
    Confirm Button { margin-left: 1; }
    """

    def __init__(self, title: str, body: str, yes: str = "Continue", no: str = "Cancel",
                 danger: bool = False, type_to_confirm: str | None = None) -> None:
        super().__init__(classes="danger" if danger else "")
        self.t, self.body, self.yes, self.no = title, body, yes, no
        self.danger = danger
        self.ttc = type_to_confirm

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{self.t}[/b]")
            yield Static(self.body, id="body")
            if self.ttc:
                yield Label(f"Type [b]{self.ttc}[/b] to confirm:")
                yield Input(id="ttc")
            with Horizontal():
                yield Button(self.no, id="no")
                yield Button(self.yes, id="yes", variant="error" if self.danger else "primary",
                             disabled=bool(self.ttc))

    @on(Input.Changed, "#ttc")
    def _typed(self, ev: Input.Changed) -> None:
        self.query_one("#yes", Button).disabled = ev.value.strip() != self.ttc

    @on(Button.Pressed)
    def _pressed(self, ev: Button.Pressed) -> None:
        self.dismiss(ev.button.id == "yes")

    def key_escape(self) -> None:
        self.dismiss(False)


class Message(ModalScreen[None]):
    DEFAULT_CSS = """
    Message { align: center middle; }
    Message > Vertical { width: 80; max-width: 95%; height: auto; max-height: 90%; border: thick $primary; background: $surface; padding: 1 2; }
    Message.error > Vertical { border: thick $error; }
    Message VerticalScroll { height: auto; max-height: 30; }
    Message Horizontal { height: auto; align-horizontal: right; margin-top: 1; }
    """

    def __init__(self, title: str, body: str, error: bool = False) -> None:
        super().__init__(classes="error" if error else "")
        self.t, self.body = title, body

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{self.t}[/b]")
            with VerticalScroll():
                yield Static(self.body, markup=False)
            with Horizontal():
                yield Button("OK", id="ok", variant="primary")

    def on_button_pressed(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class Prompt(ModalScreen[Optional[str]]):
    DEFAULT_CSS = """
    Prompt { align: center middle; }
    Prompt > Vertical { width: 72; max-width: 95%; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    Prompt Horizontal { height: auto; align-horizontal: right; margin-top: 1; }
    Prompt Button { margin-left: 1; }
    """

    def __init__(self, title: str, label: str, value: str = "", password: bool = False) -> None:
        super().__init__()
        self.t, self.label, self.value, self.password = title, label, value, password

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{self.t}[/b]")
            yield Label(self.label)
            yield Input(self.value, id="val", password=self.password)
            with Horizontal():
                yield Button("Cancel", id="no")
                yield Button("OK", id="yes", variant="primary")

    @on(Input.Submitted)
    def _sub(self, ev: Input.Submitted) -> None:
        self.dismiss(ev.value)

    @on(Button.Pressed)
    def _pressed(self, ev: Button.Pressed) -> None:
        self.dismiss(self.query_one("#val", Input).value if ev.button.id == "yes" else None)

    def key_escape(self) -> None:
        self.dismiss(None)


class Job(ModalScreen[Any]):
    """Runs ``fn(progress, cancel)`` in a thread with a progress bar and Cancel button.

    Dismisses with the function's return value, or ``JobFailed``/``Cancelled`` instances.
    """

    DEFAULT_CSS = """
    Job { align: center middle; }
    Job > Vertical { width: 76; max-width: 95%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    Job #msg { height: 2; margin: 1 0; }
    Job ProgressBar { width: 100%; }
    Job Horizontal { height: auto; align-horizontal: right; margin-top: 1; }
    """

    def __init__(self, title: str, fn: Callable[[Callable[[ProgressEvent], None], CancelToken], Any],
                 cancellable: bool = True) -> None:
        super().__init__()
        self.t = title
        self.fn = fn
        self.token = CancelToken()
        self.cancellable = cancellable

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{self.t}[/b]")
            yield Static("Starting…", id="msg", markup=False)
            yield ProgressBar(id="bar", show_eta=True)
            with Horizontal():
                yield Button("Cancel", id="cancel", variant="warning", disabled=not self.cancellable)

    def on_mount(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _progress(self, ev: ProgressEvent) -> None:
        self.app.call_from_thread(self._update, ev)

    def _update(self, ev: ProgressEvent) -> None:
        bar = self.query_one("#bar", ProgressBar)
        if ev.total > 0:
            bar.update(total=ev.total, progress=ev.done)
        else:
            bar.update(total=None)
        if ev.message:
            extra = f"  ({human_size(ev.done)} / {human_size(ev.total)})" if ev.total > 10_000_000 else ""
            self.query_one("#msg", Static).update(ev.message + extra)

    def _run(self) -> None:
        try:
            result = self.fn(self._progress, self.token)
        except Cancelled:
            result = Cancelled()
        except BaseException as e:  # noqa: BLE001 - shown to the user
            result = JobFailed(e, traceback.format_exc())
        self.app.call_from_thread(self.dismiss, result)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.token.cancel()
        self.query_one("#msg", Static).update("Cancelling…")
        self.query_one("#cancel", Button).disabled = True


class JobFailed:
    def __init__(self, exc: BaseException, tb: str) -> None:
        self.exc = exc
        self.tb = tb

    def __str__(self) -> str:
        return str(self.exc) or type(self.exc).__name__


def _roots() -> list[Path]:
    if sys.platform == "win32":
        return [Path(f"{d}:\\") for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    roots = [Path.home(), Path("/")]
    for base in ("/media", "/run/media", "/mnt", "/Volumes"):
        b = Path(base)
        if b.exists():
            try:
                for p in b.iterdir():
                    if p.is_dir():
                        roots.append(p)
                        if base in ("/media", "/run/media"):
                            roots += [q for q in p.iterdir() if q.is_dir()]
            except OSError:
                pass
    return roots


class _FilteredTree(DirectoryTree):
    def __init__(self, path: Path, exts: set[str] | None, dirs_only: bool, **kw) -> None:
        super().__init__(path, **kw)
        self.exts = exts
        self.dirs_only = dirs_only

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        out = []
        for p in paths:
            if p.name.startswith("."):
                continue
            try:
                if p.is_dir():
                    out.append(p)
                elif not self.dirs_only and (not self.exts or p.suffix.lower() in self.exts):
                    out.append(p)
            except OSError:
                pass
        return out


class PickPath(ModalScreen[Optional[list[Path]]]):
    """File or folder picker. Type a path or browse. Space marks multiple files."""

    DEFAULT_CSS = """
    PickPath { align: center middle; }
    PickPath > Vertical { width: 100; max-width: 98%; height: 90%; border: thick $primary; background: $surface; padding: 0 1; }
    PickPath #roots { height: auto; }
    PickPath #roots Button { min-width: 6; margin-right: 1; }
    PickPath DirectoryTree { height: 1fr; }
    PickPath #picked { height: auto; max-height: 4; color: $text-muted; }
    PickPath Horizontal#btns { height: auto; align-horizontal: right; }
    PickPath #btns Button { margin-left: 1; }
    """

    BINDINGS = [("space", "mark", "Mark/unmark"), ("escape", "cancel", "Cancel")]

    def __init__(self, title: str, start: Path | None = None, exts: set[str] | None = None,
                 folder: bool = False, multiple: bool = False) -> None:
        super().__init__()
        self.t = title
        self.start = start if start and start.exists() else Path.home()
        self.exts = exts
        self.folder = folder
        self.multiple = multiple
        self.marked: list[Path] = []
        self.current: Path | None = None

    def compose(self) -> ComposeResult:
        hint = "Choose a folder, then press Select." if self.folder else (
            "Enter opens folders. Space marks files (pick several), then press Select."
            if self.multiple else "Pick a file, then press Select.")
        with Vertical():
            yield Label(f"[b]{self.t}[/b]  [dim]{hint}[/dim]")
            with Horizontal(id="roots"):
                for i, r in enumerate(_roots()[:8]):
                    yield Button(str(r) if len(str(r)) < 18 else r.name, id=f"root{i}", classes="root")
            yield Input(str(self.start), id="path", placeholder="Type or paste a path")
            yield _FilteredTree(self.start, self.exts, self.folder, id="tree")
            yield Static("", id="picked", markup=False)
            with Horizontal(id="btns"):
                yield Button("Cancel", id="cancel")
                yield Button("Select", id="select", variant="primary")
        self._roots = _roots()[:8]

    @on(Button.Pressed, ".root")
    def _root(self, ev: Button.Pressed) -> None:
        i = int(ev.button.id[4:])
        self._goto(self._roots[i])

    def _goto(self, p: Path) -> None:
        tree = self.query_one("#tree", _FilteredTree)
        tree.path = p
        self.query_one("#path", Input).value = str(p)
        self.current = p

    @on(Input.Submitted, "#path")
    def _typed(self, ev: Input.Submitted) -> None:
        p = Path(ev.value.strip().strip('"')).expanduser()
        if p.is_dir():
            self._goto(p)
        elif p.is_file():
            self.dismiss([p])

    @on(DirectoryTree.DirectorySelected)
    def _dir(self, ev: DirectoryTree.DirectorySelected) -> None:
        self.current = ev.path
        self.query_one("#path", Input).value = str(ev.path)

    @on(DirectoryTree.FileSelected)
    def _file(self, ev: DirectoryTree.FileSelected) -> None:
        self.current = ev.path
        self.query_one("#path", Input).value = str(ev.path)
        if not self.multiple and not self.folder:
            self.dismiss([ev.path])

    def action_mark(self) -> None:
        tree = self.query_one("#tree", _FilteredTree)
        node = tree.cursor_node
        if not node or not node.data:
            return
        p = Path(node.data.path)
        if p.is_dir() and not self.folder:
            return
        if p in self.marked:
            self.marked.remove(p)
        else:
            self.marked.append(p)
        self.query_one("#picked", Static).update(
            f"Marked ({len(self.marked)}): " + ", ".join(x.name for x in self.marked))

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#select")
    def _select(self) -> None:
        if self.marked:
            self.dismiss(list(self.marked))
            return
        p = Path(self.query_one("#path", Input).value.strip().strip('"')).expanduser()
        if self.folder:
            if p.is_dir():
                self.dismiss([p])
            else:
                self.app.notify("Choose a folder", severity="warning")
        else:
            if p.is_file():
                self.dismiss([p])
            elif p.is_dir() and self.multiple and self.exts:
                files = sorted(x for x in p.iterdir() if x.is_file() and x.suffix.lower() in self.exts)
                if files:
                    self.dismiss(files)
                else:
                    self.app.notify("No matching files in this folder", severity="warning")
            else:
                self.app.notify("Choose a file", severity="warning")
