"""Reusable dialogs and widgets: confirm, either/or choice, message, job progress (with
cancel), file/folder picker, text prompt, and the markable table the list panes share."""
from __future__ import annotations

import os
import string
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Optional
from collections.abc import Callable, Iterable, Sequence

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message as TMessage
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, DirectoryTree, Input, Label, ProgressBar,
                             SelectionList, Static)

from ..core.drives import human_size  # noqa: F401 - the one human_size; panes import it from here
from ..core.progress import UNIT_BYTES, Cancelled, CancelToken, ProgressEvent


def cursor_key(table: DataTable) -> Optional[str]:
    """Row key under the cursor of a row-cursor table, or None when it is empty."""
    if not table.row_count:
        return None
    try:
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
    except Exception:  # noqa: BLE001 - cursor briefly out of range while a table refills
        return None


class MarkTable(DataTable):
    """A row table whose rows can be ticked with Space.

    ``selected_keys()`` is what actions work on: the ticked rows that are
    *currently shown*, in the order shown - or the highlighted row when nothing
    is ticked. Rows that are filtered out are never part of it.
    """

    BINDINGS = [Binding("space", "toggle_mark", "Mark")]

    def __init__(self, **kw: Any) -> None:
        kw.setdefault("cursor_type", "row")
        kw.setdefault("zebra_stripes", True)
        super().__init__(**kw)
        self.marked: set[str] = set()
        self._mark_col: Any = None

    def set_columns(self, *labels: str) -> None:
        self._mark_col = self.add_columns(" ", *labels)[0]

    def row_keys(self) -> list[str]:
        return [str(r.key.value) for r in self.ordered_rows]

    def current_key(self) -> Optional[str]:
        return cursor_key(self)

    def fill(self, rows: Iterable[tuple[str, Sequence[Any]]]) -> None:
        """Replace the rows with ``(key, cells)`` pairs, keeping the highlight on the
        same row where possible. Ticks on rows that are no longer shown are dropped."""
        keep, keep_row = self.current_key(), self.cursor_row
        self.clear()
        keys: list[str] = []
        for key, cells in rows:
            if key in keys:
                continue       # never crash on a repeated key
            keys.append(key)
            self.add_row("✔" if key in self.marked else "", *cells, key=key)
        self.marked &= set(keys)
        if keys:
            self.move_cursor(row=keys.index(keep) if keep in keys else min(keep_row, len(keys) - 1))

    def selected_keys(self) -> list[str]:
        keys = self.row_keys()
        picked = [k for k in keys if k in self.marked]
        if picked:
            return picked
        cur = self.current_key()
        return [cur] if cur is not None else []

    def clear_marks(self) -> None:
        for k in self.row_keys():
            if k in self.marked:
                self.update_cell(k, self._mark_col, "")
        self.marked.clear()

    def action_toggle_mark(self) -> None:
        key = self.current_key()
        if key is None:
            return
        self.marked.symmetric_difference_update({key})
        self.update_cell(key, self._mark_col, "✔" if key in self.marked else "")
        if self.cursor_row + 1 < self.row_count:
            self.move_cursor(row=self.cursor_row + 1)


class Confirm(ModalScreen[Optional[bool]]):
    """Yes/No dialog: True for yes, False for the "no" button, None for Escape.

    If ``type_to_confirm`` is set the user must type it first.
    """

    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical { width: 72; max-width: 95%; height: auto; max-height: 95%; border: thick $warning; background: $surface; padding: 1 2; }
    Confirm.danger > Vertical { border: thick $error; }
    Confirm VerticalScroll { height: auto; max-height: 30; margin-bottom: 1; }
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
            with VerticalScroll():
                yield Static(self.body, id="body", markup=False)
            if self.ttc:
                yield Label("Type the text below to confirm:")
                yield Static(self.ttc, markup=False)
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
        self.dismiss(None)


class Choice(ModalScreen[Optional[str]]):
    """Either/or question with a real way out: returns "a", "b", or None (Cancel/Escape)."""

    DEFAULT_CSS = """
    Choice { align: center middle; }
    Choice > Vertical { width: 76; max-width: 95%; height: auto; max-height: 95%; border: thick $primary; background: $surface; padding: 1 2; }
    Choice VerticalScroll { height: auto; max-height: 30; margin-bottom: 1; }
    Choice Horizontal { height: auto; align-horizontal: right; }
    Choice Button { margin-left: 1; }
    """

    def __init__(self, title: str, body: str, a: str, b: str, cancel: str = "Cancel") -> None:
        super().__init__()
        self.t, self.body, self.a, self.b, self.cancel = title, body, a, b, cancel

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{self.t}[/b]")
            with VerticalScroll():
                yield Static(self.body, id="body", markup=False)
            with Horizontal():
                yield Button(self.cancel, id="cancel")
                yield Button(self.b, id="b")
                yield Button(self.a, id="a", variant="primary")

    @on(Button.Pressed)
    def _pressed(self, ev: Button.Pressed) -> None:
        self.dismiss(ev.button.id if ev.button.id in ("a", "b") else None)

    def key_escape(self) -> None:
        self.dismiss(None)


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

    def __init__(self, title: str, label: str, value: str = "") -> None:
        super().__init__()
        self.t, self.label, self.value = title, label, value

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{self.t}[/b]")
            yield Static(self.label, markup=False)
            yield Input(self.value, id="val")
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
        try:
            self.app.call_from_thread(self._update, ev)
        except RuntimeError:       # the app is closing; let the worker finish quietly
            pass

    def _update(self, ev: ProgressEvent) -> None:
        bar = self.query_one("#bar", ProgressBar)
        if ev.total > 0:
            bar.update(total=ev.total, progress=ev.done)
        else:
            bar.update(total=None)
        if ev.message:
            # sizes only for byte counts (ev.unit says so), and only when they are worth showing
            sized = ev.unit == UNIT_BYTES and ev.total > 10_000_000
            extra = f"  ({human_size(ev.done)} / {human_size(ev.total)})" if sized else ""
            self.query_one("#msg", Static).update(ev.message + extra)

    def _run(self) -> None:
        result: Any
        try:
            result = self.fn(self._progress, self.token)
        except Cancelled:
            result = Cancelled()
        except BaseException as e:  # noqa: BLE001 - shown to the user
            result = JobFailed(e, traceback.format_exc())
        try:
            self.app.call_from_thread(self.dismiss, result)
        except Exception:  # noqa: BLE001 - the app is closing
            pass

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
    roots: list[Path] = [Path.home(), Path("/")]
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
    """Directory tree with a file filter. Space asks the picker to tick the
    highlighted file (the stock tree uses Space to expand/collapse, which hid it)."""

    BINDINGS = [Binding("space", "mark", "Mark/unmark")]

    class MarkRequested(TMessage):
        pass

    def __init__(self, path: Path, exts: set[str] | None, dirs_only: bool, **kw: Any) -> None:
        super().__init__(path, **kw)
        self.exts = exts
        self.dirs_only = dirs_only

    def action_mark(self) -> None:
        self.post_message(self.MarkRequested())

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        out = []
        for p in paths:
            if p.name.startswith("."):
                continue
            try:
                if p.is_dir() or not self.dirs_only and (not self.exts or p.suffix.lower() in self.exts):
                    out.append(p)
            except OSError:
                pass
        return out


def _clean_path(text: str) -> Path:
    return Path(text.strip().strip('"')).expanduser()


class PickPath(ModalScreen[Optional[list[Path]]]):
    """File or folder picker. Type a path or browse.

    Modes: a single file (default); ``multiple`` files (Space ticks them);
    ``folder``; ``either`` a matching file or a folder; ``any_path`` - anything
    that exists and isn't a folder, including device nodes such as ``/dev/sdb``
    or ``\\\\.\\PhysicalDrive2`` that can only be typed.
    """

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

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, start: Path | None = None, exts: set[str] | None = None,
                 folder: bool = False, multiple: bool = False, either: bool = False,
                 any_path: bool = False) -> None:
        super().__init__()
        self.t = title
        try:
            ok = bool(start and start.is_dir())
        except OSError:
            ok = False
        self.start = start if ok and start else Path.home()
        self.exts = exts
        self.folder = folder
        self.multiple = multiple and not folder
        self.either = either
        self.any_path = any_path
        self.marked: list[Path] = []
        self.current: Path | None = None
        self._root_paths: list[Path] = []

    def compose(self) -> ComposeResult:
        if self.folder:
            hint = "Choose a folder, then press Select."
        elif self.multiple:
            hint = "Enter opens folders. Space ticks files (pick several), then press Select."
        elif self.either:
            hint = "Pick a file or a folder, then press Select."
        elif self.any_path:
            hint = "Pick a file, or type a device path, then press Select."
        else:
            hint = "Pick a file, then press Select."
        self._root_paths = _roots()[:8]
        with Vertical():
            yield Label(f"[b]{self.t}[/b]  [dim]{hint}[/dim]")
            with Horizontal(id="roots"):
                for i, r in enumerate(self._root_paths):
                    yield Button(str(r) if len(str(r)) < 18 else r.name, id=f"root{i}", classes="root")
            yield Input(str(self.start), id="path", placeholder="Type or paste a path")
            yield _FilteredTree(self.start, self.exts, self.folder, id="tree")
            yield Static("", id="picked", markup=False)
            with Horizontal(id="btns"):
                yield Button("Cancel", id="cancel")
                yield Button("Select", id="select", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#tree", _FilteredTree).focus()

    @on(Button.Pressed, ".root")
    def _root(self, ev: Button.Pressed) -> None:
        self._goto(self._root_paths[int((ev.button.id or "root0")[4:])])

    def _goto(self, p: Path) -> None:
        tree = self.query_one("#tree", _FilteredTree)
        tree.path = p
        self.query_one("#path", Input).value = str(p)
        self.current = p

    def _is_file(self, p: Path) -> bool:
        try:
            if self.any_path:
                return p.exists() and not p.is_dir()
            return p.is_file()
        except OSError:
            return False

    @staticmethod
    def _is_dir(p: Path) -> bool:
        try:
            return p.is_dir()
        except OSError:
            return False

    @on(Input.Submitted, "#path")
    def _typed(self, ev: Input.Submitted) -> None:
        p = _clean_path(ev.value)
        if self._is_dir(p):
            self._goto(p)
        elif self._is_file(p) and not self.folder:
            self.dismiss([p])
        else:
            self.app.notify("Nothing found at that path", severity="warning")

    @on(DirectoryTree.DirectorySelected)
    def _dir(self, ev: DirectoryTree.DirectorySelected) -> None:
        self.current = ev.path
        self.query_one("#path", Input).value = str(ev.path)

    @on(DirectoryTree.FileSelected)
    def _file(self, ev: DirectoryTree.FileSelected) -> None:
        self.current = ev.path
        self.query_one("#path", Input).value = str(ev.path)
        if not self.multiple and not self.folder and not self.either:
            self.dismiss([ev.path])

    @on(_FilteredTree.MarkRequested)
    def _mark_requested(self) -> None:
        self.action_mark()

    def action_mark(self) -> None:
        if not self.multiple:
            return
        tree = self.query_one("#tree", _FilteredTree)
        node = tree.cursor_node
        if not node or not node.data:
            return
        p = Path(node.data.path)
        if self._is_dir(p):
            if node.allow_expand:
                node.toggle()
            return
        if p in self.marked:
            self.marked.remove(p)
        else:
            self.marked.append(p)
        self.query_one("#picked", Static).update(
            (f"Ticked ({len(self.marked)}): " + ", ".join(x.name for x in self.marked)) if self.marked else "")
        tree.action_cursor_down()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def _matching_files(self, folder: Path) -> list[Path]:
        try:
            return sorted(x for x in folder.iterdir()
                          if x.is_file() and x.suffix.lower() in (self.exts or set()))
        except OSError:
            return []

    @on(Button.Pressed, "#select")
    def _select(self) -> None:
        if self.marked:
            self.dismiss(list(self.marked))
            return
        p = _clean_path(self.query_one("#path", Input).value)
        if self.folder:
            if self._is_dir(p):
                self.dismiss([p])
            else:
                self.app.notify("Choose a folder", severity="warning")
        elif self.either:
            if self._is_dir(p) or (self._is_file(p) and (not self.exts or p.suffix.lower() in self.exts)):
                self.dismiss([p])
            else:
                self.app.notify("Choose a folder or a matching file", severity="warning")
        elif self._is_file(p):
            self.dismiss([p])
        elif self._is_dir(p) and self.multiple and self.exts:
            files = self._matching_files(p)
            if files:
                self.dismiss(files)
            else:
                self.app.notify("No matching files in this folder", severity="warning")
        else:
            self.app.notify("Choose a file or type a device path" if self.any_path else "Choose a file",
                            severity="warning")


class CheatPicker(ModalScreen[Optional[tuple[list[int], dict[str, dict[str, str]]]]]):
    """Tick cheats for one game. Returns (selected indexes, placeholder values) or None.

    Values are keyed by cheat name, which is how ``cheats.build_gct`` looks them up.
    """

    DEFAULT_CSS = """
    CheatPicker { align: center middle; }
    CheatPicker > Vertical { width: 110; max-width: 98%; height: 90%; border: thick $primary; background: $surface; padding: 0 1; }
    CheatPicker SelectionList { height: 1fr; }
    CheatPicker #info { height: 6; color: $text-muted; }
    CheatPicker #vals { height: auto; max-height: 10; }
    CheatPicker Horizontal#btns { height: auto; align-horizontal: right; }
    CheatPicker #btns Button { margin-left: 1; }
    """

    def __init__(self, title: str, cheats: list, preselected: list[int]) -> None:
        super().__init__()
        self.t = title
        self.cheats = cheats
        self.pre = set(preselected)
        self._typed: dict[tuple[int, str], str] = {}     # (cheat index, placeholder) -> text
        self._shown: list[tuple[int, str]] = []

    @staticmethod
    def _vid(i: int, letter: str) -> str:
        return f"v{i}_{ord(letter[:1] or '?')}"       # placeholders can be any character

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]Cheats – {self.t}[/b]  [dim]Space ticks a cheat. Cheats marked ✎ need a value.[/dim]",
                        markup=True)
            yield SelectionList(*[((("✎ " if c.needs_values else "") + c.name), i, i in self.pre)
                                  for i, c in enumerate(self.cheats)], id="list")
            yield Static("", id="info", markup=False)
            yield VerticalScroll(id="vals")
            with Horizontal(id="btns"):
                yield Button("Cancel", id="cancel")
                yield Button("No cheats", id="none")
                yield Button("Save", id="save", variant="primary")

    async def on_mount(self) -> None:
        await self._refresh_values()

    def on_selection_list_selection_highlighted(self, ev: SelectionList.SelectionHighlighted) -> None:
        c = self.cheats[ev.selection.value]
        codes = "\n".join(f"{a} {b}" for a, b in c.codes[:4]) + ("\n…" if len(c.codes) > 4 else "")
        self.query_one("#info", Static).update((c.comment or "") + "\n" + codes)

    async def on_selection_list_selected_changed(self) -> None:
        await self._refresh_values()

    def _remember(self) -> None:
        for i, letter in self._shown:
            try:
                self._typed[(i, letter)] = self.query_one("#" + self._vid(i, letter), Input).value
            except Exception:  # noqa: BLE001 - not mounted (yet)
                pass

    async def _refresh_values(self) -> None:
        """Show one value box per placeholder of the ticked cheats, keeping what was typed."""
        sel = sorted(self.query_one("#list", SelectionList).selected)
        want = [(i, letter) for i in sel for letter in self.cheats[i].placeholders]
        if want == self._shown:
            return
        self._remember()
        box = self.query_one("#vals", VerticalScroll)
        await box.remove_children()
        self._shown = []
        widgets: list[Any] = []
        for i, letter in want:
            c = self.cheats[i]
            width = c.placeholders[letter]
            widgets.append(Label(f"{c.name}: value for {letter} ({width} hex digits)", markup=False))
            widgets.append(Input(self._typed.get((i, letter), ""), placeholder="0" * width,
                                 id=self._vid(i, letter), restrict=r"[0-9a-fA-F]*", max_length=width))
        if widgets:
            await box.mount(*widgets)
        self._shown = want

    @on(Button.Pressed, "#cancel")
    def _c(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#none")
    def _n(self) -> None:
        self.dismiss(([], {}))

    @on(Button.Pressed, "#save")
    def _s(self) -> None:
        self._remember()
        sel = sorted(self.query_one("#list", SelectionList).selected)
        values: dict[str, dict[str, str]] = {}
        for i in sel:
            c = self.cheats[i]
            for letter in c.placeholders:
                v = self._typed.get((i, letter), "").strip()
                if not v:
                    self.app.notify(f"Enter a value for {c.name}", severity="warning")
                    return
                values.setdefault(c.name, {})[letter] = v
        self.dismiss((sel, values))

    def key_escape(self) -> None:
        self.dismiss(None)


class PromptText(ModalScreen[Optional[str]]):
    """Multi-line text entry (paste)."""

    DEFAULT_CSS = """
    PromptText { align: center middle; }
    PromptText > Vertical { width: 100; max-width: 98%; height: 80%; border: thick $primary; background: $surface; padding: 0 1; }
    PromptText TextArea { height: 1fr; }
    PromptText Horizontal { height: auto; align-horizontal: right; }
    PromptText Button { margin-left: 1; }
    """

    def __init__(self, title: str, label: str, text: str = "") -> None:
        super().__init__()
        self.t, self.label, self.text = title, label, text

    def compose(self) -> ComposeResult:
        from textual.widgets import TextArea
        with Vertical():
            yield Label(f"[b]{self.t}[/b]")
            yield Static(self.label, markup=False)
            yield TextArea(self.text, id="ta")
            with Horizontal():
                yield Button("Cancel", id="no")
                yield Button("OK", id="yes", variant="primary")

    @on(Button.Pressed)
    def _p(self, ev: Button.Pressed) -> None:
        from textual.widgets import TextArea
        self.dismiss(self.query_one("#ta", TextArea).text if ev.button.id == "yes" else None)

    def key_escape(self) -> None:
        self.dismiss(None)


class ChoiceForm(ModalScreen[Optional[dict]]):
    """A small form of dropdowns: fields = [(key, label, [(text, value)...], current)]."""

    DEFAULT_CSS = """
    ChoiceForm { align: center middle; }
    ChoiceForm > Vertical { width: 90; max-width: 98%; height: auto; max-height: 90%; border: thick $primary; background: $surface; padding: 1 2; }
    ChoiceForm .row { height: auto; }
    ChoiceForm .row Label { width: 34; padding-top: 1; }
    ChoiceForm .row Select { width: 44; }
    ChoiceForm Horizontal#btns { height: auto; align-horizontal: right; margin-top: 1; }
    ChoiceForm #btns Button { margin-left: 1; }
    """

    def __init__(self, title: str, intro: str, fields: list) -> None:
        super().__init__()
        self.t, self.intro, self.fields = title, intro, fields

    def compose(self) -> ComposeResult:
        from textual.widgets import Select
        with Vertical():
            yield Label(f"[b]{self.t}[/b]")
            if self.intro:
                yield Static(self.intro, markup=False)
            with VerticalScroll():
                for key, label, options, current in self.fields:
                    with Horizontal(classes="row"):
                        yield Label(label)
                        vals = [v for _, v in options]
                        yield Select(options, value=current if current in vals else options[0][1],
                                     allow_blank=False, id=f"f_{key}")
            with Horizontal(id="btns"):
                yield Button("Cancel", id="no")
                yield Button("Save", id="yes", variant="primary")

    @on(Button.Pressed)
    def _p(self, ev: Button.Pressed) -> None:
        from textual.widgets import Select
        if ev.button.id != "yes":
            self.dismiss(None)
            return
        self.dismiss({k: self.query_one(f"#f_{k}", Select).value for k, *_ in self.fields})

    def key_escape(self) -> None:
        self.dismiss(None)
