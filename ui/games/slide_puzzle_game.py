# 15-puzzle: a 4x4 grid of numbered tiles plus one blank space. Click a tile
# next to the blank to slide it in. Win by getting 1-15 in order with the
# blank last. Shuffled via random VALID moves from the solved state (not a
# random permutation), which guarantees every puzzle is actually solvable.

import random

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import QWidget, QGridLayout, QPushButton, QVBoxLayout, QLabel

from base_game import MiniGameWidget
import theme
import game_theme

GRID_N = 4
SHUFFLE_MOVES = 120
TILE_PX = 74


class SlidePuzzleGame(MiniGameWidget):
    NAME = "15-Slide Puzzle"
    game_finished = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ended = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        self.status_label = QLabel("Slide the tiles into order")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        self.board_frame = QWidget()
        self.board_frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        board_layout = QVBoxLayout(self.board_frame)
        board_layout.setContentsMargins(8, 8, 8, 8)
        self.grid_widget = QWidget()
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setSpacing(5)
        board_layout.addWidget(self.grid_widget)
        outer.addWidget(self.board_frame, 0, Qt.AlignmentFlag.AlignHCenter)

        # board[row][col] -> tile number 1..15, or None for the blank
        self.board = [[r * GRID_N + c + 1 for c in range(GRID_N)] for r in range(GRID_N)]
        self.board[GRID_N - 1][GRID_N - 1] = None
        self.blank = (GRID_N - 1, GRID_N - 1)

        self.buttons = {}
        for r in range(GRID_N):
            for c in range(GRID_N):
                button = QPushButton()
                button.setFixedSize(TILE_PX, TILE_PX)
                button.clicked.connect(lambda _checked, row=r, col=c: self._on_tile_clicked(row, col))
                self.grid_layout.addWidget(button, r, c)
                self.buttons[(r, c)] = button

        self._shuffle()
        self.on_theme_changed(theme.manager.colors)  # first paint, also refreshes tile text/enabled state

    def on_theme_changed(self, colors):
        self.board_frame.setStyleSheet(game_theme.board_frame_style(colors))
        self.status_label.setStyleSheet(game_theme.status_style(colors, size=14))
        self._tile_style = game_theme.button_style(colors, size=20)
        self._blank_style = f"background-color: {colors['bg']}; border: none;"
        self._refresh()

    def _shuffle(self):
        # Random walk of valid slides from the solved state -- always
        # solvable, and different every time (random.choice), so the same
        # scramble essentially never repeats.
        for _ in range(SHUFFLE_MOVES):
            neighbors = self._blank_neighbors()
            move = random.choice(neighbors)
            self._swap(move, self.blank)
            self.blank = move

    def _blank_neighbors(self):
        r, c = self.blank
        candidates = [(r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)]
        return [(nr, nc) for nr, nc in candidates if 0 <= nr < GRID_N and 0 <= nc < GRID_N]

    def _swap(self, a, b):
        ar, ac = a
        br, bc = b
        self.board[ar][ac], self.board[br][bc] = self.board[br][bc], self.board[ar][ac]

    def _on_tile_clicked(self, row, col):
        if self._ended:
            return
        if (row, col) not in self._blank_neighbors():
            return
        self._swap((row, col), self.blank)
        self.blank = (row, col)
        self._refresh()
        if self._is_solved():
            self._ended = True
            self.status_label.setText("Solved!")
            self.game_finished.emit(True)

    def _is_solved(self):
        expected = [[r * GRID_N + c + 1 for c in range(GRID_N)] for r in range(GRID_N)]
        expected[GRID_N - 1][GRID_N - 1] = None
        return self.board == expected

    def _refresh(self):
        for (r, c), button in self.buttons.items():
            value = self.board[r][c]
            if value is None:
                button.setText("")
                button.setEnabled(False)
                button.setStyleSheet(self._blank_style)
            else:
                button.setText(str(value))
                button.setEnabled(True)
                button.setStyleSheet(self._tile_style)
