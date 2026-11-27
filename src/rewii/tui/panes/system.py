from textual.widgets import Label
from ..base import Pane


class SystemPane(Pane):
    def compose(self):
        yield Label("Coming soon", classes="title")
