# Memory Match: a 4x4 grid of face-down cards (8 symbol pairs). Click two;
# a match stays revealed, a mismatch flips back after a brief pause. Win
# once every pair is matched.

import random

from PyQt6.QtCore import pyqtSignal, QTimer, Qt
from PyQt6.QtWidgets import QWidget, QGridLayout, QPushButton, QVBoxLayout, QLabel

from base_game import MiniGameWidget
import theme
import game_theme

GRID_N = 4
SYMBOLS = ["*", "#", "@", "%", "+", "=", "&", "$"]  # 8 symbols -> 8 pairs -> 16 cards
CARD_PX = 74
MISMATCH_DELAY_MS = 650


class MemoryMatchGame(MiniGameWidget):
    NAME = "Memory Match"
    game_finished = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ended = False
        self._locked = False       # True while a mismatched pair is briefly shown before flipping back
        self._revealed = []        # positions currently face-up this turn (0, 1, or 2 entries)
        self._matched = set()      # positions already permanently matched

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        self.status_label = QLabel("Find every matching pair")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        self.board_frame = QWidget()
        self.board_frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        board_layout = QVBoxLayout(self.board_frame)
        board_layout.setContentsMargins(8, 8, 8, 8)
        grid_widget = QWidget()
        grid_layout = QGridLayout(grid_widget)
        grid_layout.setSpacing(5)
        board_layout.addWidget(grid_widget)
        outer.addWidget(self.board_frame, 0, Qt.AlignmentFlag.AlignHCenter)

        cards = SYMBOLS + SYMBOLS
        random.shuffle(cards)
        self.cards = {}  # (r, c) -> symbol
        self.buttons = {}
        for i, symbol in enumerate(cards):
            r, c = divmod(i, GRID_N)
            self.cards[(r, c)] = symbol
            button = QPushButton("?")
            button.setFixedSize(CARD_PX, CARD_PX)
            button.clicked.connect(lambda _checked, pos=(r, c): self._on_card_clicked(pos))
            grid_layout.addWidget(button, r, c)
            self.buttons[(r, c)] = button

        self.on_theme_changed(theme.manager.colors)

    def on_theme_changed(self, colors):
        self.board_frame.setStyleSheet(game_theme.board_frame_style(colors))
        self.status_label.setStyleSheet(game_theme.status_style(colors, size=14))
        self._face_down_style = game_theme.button_style(colors, size=22)
        # A distinctly lighter, accent-tinted fill for a flipped-but-not-
        # yet-matched card -- so "just revealed" reads differently from
        # "permanently matched" at a glance, not just by text alone.
        revealed_bg = game_theme.light_tint(colors, base_key="accent", weight=0.6)
        self._face_up_style = (
            f"font-size: 22px; font-weight: bold; background-color: {revealed_bg}; "
            f"border: 2px solid {colors['accent']}; border-radius: 6px; color: #111111; padding: 0px;"
        )
        self._matched_style = (
            f"font-size: 22px; font-weight: bold; background-color: {colors['accent']}; "
            f"border: 2px solid {colors['accent']}; border-radius: 6px; color: {colors['bg']}; padding: 0px;"
        )
        for pos in self.buttons:
            self._style_card(pos)

    def _style_card(self, pos):
        button = self.buttons[pos]
        if pos in self._matched:
            button.setStyleSheet(self._matched_style)
        elif pos in self._revealed:
            button.setStyleSheet(self._face_up_style)
        else:
            button.setStyleSheet(self._face_down_style)

    def _on_card_clicked(self, pos):
        if self._ended or self._locked or pos in self._matched or pos in self._revealed:
            return

        self.buttons[pos].setText(self.cards[pos])
        self._revealed.append(pos)
        self._style_card(pos)

        if len(self._revealed) < 2:
            return

        first, second = self._revealed
        if self.cards[first] == self.cards[second]:
            self._matched.add(first)
            self._matched.add(second)
            self._revealed = []
            self._style_card(first)
            self._style_card(second)
            self.status_label.setText(f"Matched {len(self._matched) // 2} of {len(SYMBOLS)}")
            if len(self._matched) == len(SYMBOLS) * 2:
                self._ended = True
                self.status_label.setText("All matched!")
                self.game_finished.emit(True)
        else:
            self._locked = True
            QTimer.singleShot(MISMATCH_DELAY_MS, self._flip_back)

    def _flip_back(self):
        for pos in self._revealed:
            self.buttons[pos].setText("?")
        self._revealed = []
        for pos in self.buttons:
            if pos not in self._matched:
                self._style_card(pos)
        self._locked = False
