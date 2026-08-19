# Shared interface every mini-game widget implements, so the break-time
# game host (ui/overlay.py) can construct, swap, and listen to any of them
# identically without knowing which specific game it is.
#
# A game widget:
#   - is playable the moment it's constructed (no separate "start" call)
#   - emits game_finished(won) exactly once, when a round ends (win OR
#     lose) -- the host then swaps in a fresh, randomly-picked game so play
#     continues until the break's timer runs out
#   - has a NAME class attribute for display
#   - is a QWidget, so the host can just addWidget() it; games needing
#     keyboard input (Snake) should call self.setFocusPolicy(...) themselves
#   - automatically gets a real, themed panel shell (see game_theme.py) --
#     a bordered/padded "card" using the app's CURRENT theme, live-updating
#     if the theme changes -- instead of bare, borderless content that
#     ignores whichever theme the user picked. A subclass only needs to
#     override on_theme_changed() if it has its own internal cells/tiles
#     that also need re-coloring on a theme switch (see sudoku_game.py /
#     minesweeper_game.py for examples); the shared panel itself is handled
#     here for every game automatically.

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget

import theme
import game_theme


class MiniGameWidget(QWidget):
    NAME = "Game"

    # game_finished = pyqtSignal(bool) -- declared per-subclass import site
    # instead of here so each file's imports stay self-contained; every
    # subclass must define exactly this signal.

    def __init__(self, parent=None):
        super().__init__(parent)
        # A plain QWidget doesn't paint its own stylesheet background/
        # border/radius without this -- silently a no-op otherwise.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._apply_panel_style(theme.manager.colors)
        theme.manager.changed.connect(self._on_base_theme_changed)

    def _apply_panel_style(self, colors):
        self.setStyleSheet(game_theme.panel_style(colors))

    def _on_base_theme_changed(self, theme_id, colors):
        self._apply_panel_style(colors)
        self.on_theme_changed(colors)

    def on_theme_changed(self, colors):
        """Override in a subclass that has its own theme-derived styling
        (grid cells, tiles, etc.) beyond the shared panel shell -- called
        every time the app's theme changes, after the panel restyles."""
