from textual.widgets import Label
from ..base import Pane


class BackupsPane(Pane):
    def compose(self):
        yield Label("Coming soon", classes="title")
