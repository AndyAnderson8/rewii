"""Home: what's going on, and the first-run setup wizard."""
from __future__ import annotations

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Label, Markdown

from ..base import Pane

WELCOME = """\
**ReWii** manages everything on the computer side of a softmodded Wii
(one with the Homebrew Channel). Everything lives on **one USB drive**:

- **Games** – Wii and GameCube games, covers, cheats
- **Channels** – put any Wii game on the Wii Menu, with a cover or black launch screen
- **Homebrew** – install and update apps from the Open Shop Channel
- **Saves, backups** – back up saves, the whole drive, and your Wii's NAND
- **System & Guides** – step-by-step help for the things done on the Wii itself

**Typical first time:** Drive → choose your USB drive → *Prepare* (format FAT32) →
*Set up* (installs USB Loader GX, Nintendont, YAWMME…) → Games → *Add games*.

On the Wii, plug the drive into the **back USB port closest to the edge**.
"""


class HomePane(Pane):
    TITLE = "Home"

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("Welcome to ReWii", classes="title")
            yield Markdown(WELCOME)
            yield Label("", id="status")
            with Horizontal(classes="toolbar"):
                yield Button("Choose drive", id="go-drive", variant="primary")
                yield Button("Games", id="go-games")
                yield Button("Homebrew", id="go-homebrew")
                yield Button("Back up", id="go-backups")
                yield Button("Guides", id="go-system")

    def reload(self) -> None:
        d = self.layout_
        lines = [f"Library on this computer: {self.settings.library}"]
        if d:
            lines.append(f"Wii drive: {d.root}" + ("" if d.root.exists() else "  (not connected!)"))
        else:
            lines.append("Wii drive: none selected yet")
        prof = self.settings.console_type
        lines.append("Console: " + (f"{prof} {self.settings.console_region}" if prof else
                                    "not set (only needed for system features - see Settings)"))
        self.query_one("#status", Label).update("\n".join(lines))

    @on(Button.Pressed)
    def _go(self, ev: Button.Pressed) -> None:
        if ev.button.id and ev.button.id.startswith("go-"):
            self.rapp.action_section(ev.button.id[3:])

    @work(exclusive=True, group="home")
    async def first_run(self) -> None:
        s = self.settings
        await self.message(
            "Welcome!",
            "ReWii keeps a library of your games, channels, saves and backups on this computer, "
            f"in:\n\n  {s.library}\n\nYou can change that in Settings. Next, pick your Wii's USB drive "
            "in the Drive section.\n\nNothing is ever written to a drive without asking you first.")
        s.first_run_done = True
        s.save()
        self.rapp.action_section("drive")
