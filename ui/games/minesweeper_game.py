# Minesweeper: an 8x8 grid, 10 mines. Left-click reveals a cell (flood-filling
# outward through zero-neighbor cells); right-click toggles a flag. Mines are
# placed only after the first click, avoiding that cell and its neighbors, so
# the opening click is never an instant loss. Win by revealing every safe cell.
#
# Unrevealed cells and the board frame are fully theme-driven (see
# game_theme.py). Revealed cells are the one exception with a real
# constraint: the classic adjacent-mine-count color convention (1=blue,
# ..., 7=near-black, 8=gray) only reads correctly against a genuinely
# LIGHT surface -- so revealed cells use game_theme.light_tint(), a color
# mixed toward white FROM the theme's own accent (not an arbitrary fixed
# hex), guaranteeing legibility while still visibly shifting with the
# theme. Same idea for the exploded-mine background, mixed from the
# mine's own alert red.

import random

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import QWidget, QGridLayout, QPushButton, QVBoxLayout, QLabel

from base_game import MiniGameWidget
import theme
import game_theme

GRID_N = 8
MINE_COUNT = 10
CELL_PX = 40
MINE_ALERT_HEX = "#EF4444"  # fixed -- "danger" stays a universal red regardless of theme, like real Minesweeper

NUMBER_COLORS = {
    1: "#3B82F6", 2: "#22C55E", 3: "#EF4444", 4: "#8B5CF6",
    5: "#F59E0B", 6: "#06B6D4", 7: "#111111", 8: "#6B7280",
}


class MinesweeperGame(MiniGameWidget):
    NAME = "Minesweeper"
    game_finished = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ended = False
        self._mines_placed = False
        self.mines = set()             # (r, c) positions once placed
        self.flagged = set()
        self.revealed = set()
        self.exploded = set()          # mines actually shown after a loss
        self.adjacent = {}             # (r, c) -> mine count, filled once mines are placed
        self._revealed_bg = "#E5E7EB"  # real values assigned by on_theme_changed() below
        self._exploded_bg = "#FEE2E2"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        self.status_label = QLabel(f"Left-click to reveal, right-click to flag ({MINE_COUNT} mines)")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        self.board_frame = QWidget()
        self.board_frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        board_layout = QVBoxLayout(self.board_frame)
        board_layout.setContentsMargins(8, 8, 8, 8)
        grid_widget = QWidget()
        self.grid_layout = QGridLayout(grid_widget)
        self.grid_layout.setSpacing(2)
        board_layout.addWidget(grid_widget)
        outer.addWidget(self.board_frame, 0, Qt.AlignmentFlag.AlignHCenter)

        self.buttons = {}
        for r in range(GRID_N):
            for c in range(GRID_N):
                button = QPushButton()
                button.setFixedSize(CELL_PX, CELL_PX)
                button.clicked.connect(lambda _checked, pos=(r, c): self._on_left_click(pos))
                button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                button.customContextMenuRequested.connect(lambda _pos, pos=(r, c): self._on_right_click(pos))
                self.grid_layout.addWidget(button, r, c)
                self.buttons[(r, c)] = button

        self.on_theme_changed(theme.manager.colors)

    def on_theme_changed(self, colors):
        self.board_frame.setStyleSheet(game_theme.board_frame_style(colors))
        self.status_label.setStyleSheet(game_theme.status_style(colors, size=14))
        self._revealed_bg = game_theme.light_tint(colors, base_key="accent", weight=0.82)
        self._exploded_bg = game_theme.mix_hex(MINE_ALERT_HEX, "#FFFFFF", 0.82)

        unrevealed_style = game_theme.button_style(colors, size=14)
        for pos, button in self.buttons.items():
            if pos in self.exploded:
                self._style_exploded_cell(pos)
            elif pos in self.revealed:
                self._style_revealed_cell(pos)
            else:
                button.setStyleSheet(unrevealed_style)

    def _neighbors(self, pos):
        r, c = pos
        result = []
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = r + dr, c + dc
                if 0 <= nr < GRID_N and 0 <= nc < GRID_N:
                    result.append((nr, nc))
        return result

    def _place_mines(self, safe_pos):
        forbidden = {safe_pos} | set(self._neighbors(safe_pos))
        candidates = [
            (r, c) for r in range(GRID_N) for c in range(GRID_N)
            if (r, c) not in forbidden
        ]
        self.mines = set(random.sample(candidates, MINE_COUNT))
        for r in range(GRID_N):
            for c in range(GRID_N):
                pos = (r, c)
                if pos not in self.mines:
                    self.adjacent[pos] = sum(1 for n in self._neighbors(pos) if n in self.mines)
        self._mines_placed = True

    def _on_right_click(self, pos):
        if self._ended or pos in self.revealed:
            return
        if pos in self.flagged:
            self.flagged.remove(pos)
            self.buttons[pos].setText("")
        else:
            self.flagged.add(pos)
            self.buttons[pos].setText("F")

    def _on_left_click(self, pos):
        if self._ended or pos in self.revealed or pos in self.flagged:
            return

        if not self._mines_placed:
            self._place_mines(pos)

        if pos in self.mines:
            self._reveal_all_mines()
            self._end(won=False)
            return

        self._flood_reveal(pos)

        if len(self.revealed) == GRID_N * GRID_N - MINE_COUNT:
            self._end(won=True)

    def _flood_reveal(self, start):
        stack = [start]
        while stack:
            pos = stack.pop()
            if pos in self.revealed or pos in self.flagged:
                continue
            self.revealed.add(pos)
            button = self.buttons[pos]
            button.setEnabled(False)
            count = self.adjacent.get(pos, 0)
            if count > 0:
                button.setText(str(count))
            self._style_revealed_cell(pos)
            if count == 0:
                stack.extend(n for n in self._neighbors(pos) if n not in self.revealed)

    def _style_revealed_cell(self, pos):
        count = self.adjacent.get(pos, 0)
        button = self.buttons[pos]
        if count > 0:
            button.setStyleSheet(
                f"font-size: 14px; font-weight: bold; background-color: {self._revealed_bg}; "
                f"color: {NUMBER_COLORS.get(count, '#111111')}; padding: 0px;"
            )
        else:
            button.setStyleSheet(f"background-color: {self._revealed_bg}; padding: 0px;")

    def _style_exploded_cell(self, pos):
        self.buttons[pos].setStyleSheet(
            f"font-size: 14px; font-weight: bold; background-color: {self._exploded_bg}; "
            f"color: {MINE_ALERT_HEX}; padding: 0px;"
        )

    def _reveal_all_mines(self):
        self.exploded = set(self.mines)
        for pos in self.mines:
            self.buttons[pos].setText("*")
            self._style_exploded_cell(pos)

    def _end(self, won):
        if self._ended:
            return
        self._ended = True
        self.status_label.setText("You win!" if won else "Boom -- game over")
        for button in self.buttons.values():
            button.setEnabled(False)
        self.game_finished.emit(won)
