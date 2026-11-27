from textual.widgets import Label
from ..base import Pane


class HomebrewPane(Pane):
    def compose(self):
        yield Label("Coming soon", classes="title")
