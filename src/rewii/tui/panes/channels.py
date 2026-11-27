from textual.widgets import Label
from ..base import Pane


class ChannelsPane(Pane):
    def compose(self):
        yield Label("Coming soon", classes="title")
