# Sudoku: a 9x9 grid with ~36 given clues, the rest left for the player to
# fill in. The full solved grid is built with the classic band/stack-shuffle
# construction (a fixed number pattern, then randomly permuted) instead of
# backtracking search or the local LLM -- it's instant, always a valid
# complete grid, and a fresh shuffle every time means puzzles don't repeat.
#
# Sudoku is the one game in this library where raw keyboard typing into a
# tiny bordered grid cell isn't an obvious enough affordance on its own
# (especially full-screen, mid-break) -- so it gets a number pad (1-9) and
# an Erase button as its "tools": click a cell to select it (tracked via
# focus, highlighted), then click a digit (or Erase) to fill/clear it,
# exactly as if you'd typed it. It also gets a Notes toggle for pencil-mark
# candidates (see _toggle_note()) and a row/column spotlight on whichever
# cell is selected (see _restyle_cell()). The other games in this library
# are all already fully mouse-driven on their own (click-to-reveal/flag,
# click-to-slide, click-to-flip) or plain arrow-key control (Snake) -- no
# equivalent tool gap, so nothing was added there.
#
# Every color here is derived from the app's CURRENT theme (see
# game_theme.py) -- given/clue cells and editable cells are each a mixed
# (not alpha-blended) tint of the theme's own colors, so a row/column
# spotlight or the given/editable distinction is guaranteed visible by
# construction regardless of hue (an alpha overlay's visibility depends on
# the backdrop behind it, which silently fails in a monochrome theme -- see
# on_theme_changed()). The selected cell uses the theme's own
# selection_bg/selection_text pair (exactly what that's for), and the 3x3
# box divider is both theme-colored AND visibly wider than a plain cell
# border (several themes use the same color for accent and border, where a
# width-only difference would otherwise be invisible).

import random

from PyQt6.QtCore import Qt, QEvent, QRegularExpression, pyqtSignal
from PyQt6.QtGui import QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QWidget, QGridLayout, QLineEdit, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QStackedWidget,
)

from base_game import MiniGameWidget
import theme
import game_theme

SIDE = 9
BOX = 3
GIVEN_COUNT = 36     # clues left visible; the rest start blank
CELL_PX = 48
PAD_BUTTON_PX = 42


def _generate_full_grid():
    def shuffled(seq):
        seq = list(seq)
        random.shuffle(seq)
        return seq

    def pattern(r, c):
        return (BOX * (r % BOX) + r // BOX + c) % SIDE

    r_base = range(BOX)
    rows = [g * BOX + r for g in shuffled(r_base) for r in shuffled(r_base)]
    cols = [g * BOX + c for g in shuffled(r_base) for c in shuffled(r_base)]
    nums = shuffled(range(1, SIDE + 1))

    return [[nums[pattern(r, c)] for c in cols] for r in rows]


class SudokuGame(MiniGameWidget):
    NAME = "Sudoku"
    game_finished = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ended = False
        self._selected = None      # (row, col) of the currently selected EDITABLE cell, or None
        self._notes_mode = False
        self._notes = {}           # (row, col) -> set of candidate digits (1-9) -- editable cells only

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        self.status_label = QLabel(
            "Fill every row, column, and 3x3 box with 1-9 -- click a cell, then a number below. "
            "Notes toggles pencil-mark candidates instead of filling the cell in."
        )
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        # A distinct inset frame around the grid -- separates the actual
        # playing surface from the panel chrome (status label, pad) around
        # it, instead of the grid floating directly on the panel background.
        self.board_frame = QWidget()
        self.board_frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        board_layout = QVBoxLayout(self.board_frame)
        board_layout.setContentsMargins(8, 8, 8, 8)
        grid_widget = QWidget()
        grid_layout = QGridLayout(grid_widget)
        grid_layout.setSpacing(2)
        board_layout.addWidget(grid_widget)
        outer.addWidget(self.board_frame, 0, Qt.AlignmentFlag.AlignHCenter)

        self.solution = _generate_full_grid()
        self.grid = [row[:] for row in self.solution]

        all_positions = [(r, c) for r in range(SIDE) for c in range(SIDE)]
        random.shuffle(all_positions)
        blanked = set(all_positions[:SIDE * SIDE - GIVEN_COUNT])
        for r, c in blanked:
            self.grid[r][c] = 0

        digit_validator = QRegularExpressionValidator(QRegularExpression("[1-9]"))
        self.cells = {}          # (r, c) -> QLineEdit -- the actual value entry, always present
        self._given = set()      # positions holding a fixed clue (read-only, no notes/stack)
        self._stacks = {}        # (r, c) -> QStackedWidget([QLineEdit, notes QLabel]) -- editable cells only
        self._notes_labels = {}  # (r, c) -> QLabel showing pencil marks -- editable cells only
        for r in range(SIDE):
            for c in range(SIDE):
                cell = QLineEdit()
                cell.setFixedSize(CELL_PX, CELL_PX)
                cell.setMaxLength(1)
                cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.cells[(r, c)] = cell

                if (r, c) in blanked:
                    cell.setValidator(digit_validator)
                    cell.textChanged.connect(lambda _text, row=r, col=c: self._on_cell_changed(row, col))
                    cell.installEventFilter(self)  # tracks focus-in so the number pad knows which cell to fill

                    # A stacked pair -- the QLineEdit for a confirmed value,
                    # a small QLabel for pencil marks -- since only one can
                    # show at a time in the same grid cell (see
                    # _refresh_notes_display()).
                    stack = QStackedWidget()
                    stack.setFixedSize(CELL_PX, CELL_PX)
                    notes_label = QLabel()
                    notes_label.setFixedSize(CELL_PX, CELL_PX)
                    notes_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    notes_label.setTextFormat(Qt.TextFormat.RichText)
                    stack.addWidget(cell)        # index 0: the real value
                    stack.addWidget(notes_label)  # index 1: pencil marks
                    self._stacks[(r, c)] = stack
                    self._notes_labels[(r, c)] = notes_label
                    self._notes[(r, c)] = set()
                    grid_layout.addWidget(stack, r, c)
                else:
                    cell.setText(str(self.grid[r][c]))
                    cell.setReadOnly(True)
                    self._given.add((r, c))
                    grid_layout.addWidget(cell, r, c)

        # Number pad + Erase + Notes -- see this file's header comment for
        # why Sudoku specifically gets these. Filling via the pad goes
        # through setText()/clear(), which still fires textChanged exactly
        # like typing would, so _on_cell_changed()/_check_complete() need
        # no separate code path for pad-driven input.
        pad_widget = QWidget()
        pad_layout = QHBoxLayout(pad_widget)
        pad_layout.setSpacing(6)
        self._pad_buttons = []
        for digit in range(1, SIDE + 1):
            button = QPushButton(str(digit))
            button.setFixedSize(PAD_BUTTON_PX, PAD_BUTTON_PX)
            button.clicked.connect(lambda _checked, d=digit: self._on_pad_digit_clicked(d))
            pad_layout.addWidget(button)
            self._pad_buttons.append(button)
        self.erase_button = QPushButton("Erase")
        self.erase_button.setFixedHeight(PAD_BUTTON_PX)
        self.erase_button.clicked.connect(self._on_pad_erase_clicked)
        pad_layout.addWidget(self.erase_button)
        self.notes_button = QPushButton("Notes: Off")
        self.notes_button.setCheckable(True)
        self.notes_button.setFixedHeight(PAD_BUTTON_PX)
        self.notes_button.clicked.connect(self._on_notes_toggled)
        pad_layout.addWidget(self.notes_button)
        outer.addWidget(pad_widget)

        self.on_theme_changed(theme.manager.colors)

    def on_theme_changed(self, colors):
        """Recomputes every theme-derived color/border once, then repaints
        every cell -- called at construction and again on every live theme
        switch (see base_game.MiniGameWidget)."""
        self.board_frame.setStyleSheet(game_theme.board_frame_style(colors))
        self.status_label.setStyleSheet(game_theme.status_style(colors, size=14))

        # Mixed from surface_hover TOWARD accent (not an alpha overlay) --
        # guarantees given cells are always visibly brighter/more
        # saturated than editable cells, and a row/column spotlight always
        # visibly brighter still, all BY CONSTRUCTION regardless of hue.
        # An alpha-blended tint over the dark board looked nearly identical
        # to a plain cell in the monochrome default theme, where accent/
        # border/text are all the same green.
        self._editable_bg = colors["surface_hover"]
        self._editable_bg_lit = game_theme.mix_hex(colors["surface_hover"], colors["accent"], 0.15)
        self._given_bg = game_theme.mix_hex(colors["surface_hover"], colors["accent"], 0.35)
        self._given_bg_lit = game_theme.mix_hex(colors["surface_hover"], colors["accent"], 0.55)
        self._text_color = colors["text"]
        self._selected_overlay_str = self._selected_overlay(colors)

        thick, thin = colors["accent"], colors["border"]
        self._borders = {}
        for (r, c) in self.cells:
            right = f"4px solid {thick}" if (c + 1) % BOX == 0 and c != SIDE - 1 else f"1px solid {thin}"
            bottom = f"4px solid {thick}" if (r + 1) % BOX == 0 and r != SIDE - 1 else f"1px solid {thin}"
            self._borders[(r, c)] = f" border-right: {right}; border-bottom: {bottom};"

        for pos in self.cells:
            self._restyle_cell(pos)

        pad_style = game_theme.button_style(colors, size=17)
        for button in self._pad_buttons:
            button.setStyleSheet(pad_style)
        self.erase_button.setStyleSheet(pad_style)
        self.notes_button.setStyleSheet(game_theme.toggle_button_style(colors, size=13))

    def _selected_overlay(self, colors):
        # The theme's own selection_bg/selection_text pair -- exactly what
        # it's for -- rather than a fixed color that could clash with (or
        # simply not visually relate to) whichever theme is active.
        return f"background-color: {colors['selection_bg']}; color: {colors['selection_text']};"

    def _line_positions(self, pos):
        """Every OTHER position sharing pos's row or column."""
        r, c = pos
        return ({(r, cc) for cc in range(SIDE)} | {(rr, c) for rr in range(SIDE)}) - {pos}

    def _restyle_cell(self, pos):
        """The single source of truth for one cell's current look --
        recomputed from its CURRENT state (given/editable, in the selected
        cell's row/column or not, selected or not) rather than tracked
        incrementally, so selection changes and theme changes both funnel
        through the same, always-correct logic."""
        is_given = pos in self._given
        in_line = self._selected is not None and pos in self._line_positions(self._selected)

        if is_given:
            bg = self._given_bg_lit if in_line else self._given_bg
            weight = "font-weight: bold;"
        else:
            bg = self._editable_bg_lit if in_line else self._editable_bg
            weight = ""
        style = f"font-size: 18px; {weight} background-color: {bg}; color: {self._text_color}; padding: 0px;" + self._borders[pos]
        if pos == self._selected:
            style += self._selected_overlay_str

        self.cells[pos].setStyleSheet(style)
        label = self._notes_labels.get(pos)
        if label is not None:
            label.setStyleSheet(style)

    def _notes_html(self, pos):
        marks = self._notes.get(pos, set())
        rows_html = []
        for row in range(3):
            cells_html = []
            for col in range(3):
                digit = row * 3 + col + 1
                cells_html.append(f"<td width='33%' align='center'>{digit if digit in marks else ''}</td>")
            rows_html.append(f"<tr>{''.join(cells_html)}</tr>")
        return f"<table width='100%' height='100%' style='font-size:9px;'>{''.join(rows_html)}</table>"

    def _refresh_notes_display(self, pos):
        stack = self._stacks.get(pos)
        if stack is None:
            return
        if self.cells[pos].text():
            stack.setCurrentIndex(0)  # has a real value -- always show that, never the pencil marks
        elif self._notes.get(pos):
            self._notes_labels[pos].setText(self._notes_html(pos))
            stack.setCurrentIndex(1)
        else:
            stack.setCurrentIndex(0)  # empty, no marks -- show the (blank) QLineEdit so it's still typeable

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.FocusIn:
            for pos, cell in self.cells.items():
                if cell is obj:
                    self._select(pos)
                    break
        return super().eventFilter(obj, event)

    def _select(self, pos):
        if pos == self._selected:
            return
        affected = set()
        if self._selected is not None:
            affected |= self._line_positions(self._selected) | {self._selected}
        self._selected = pos
        affected |= self._line_positions(pos) | {pos}
        for p in affected:
            self._restyle_cell(p)

    def _on_notes_toggled(self):
        self._notes_mode = self.notes_button.isChecked()
        self.notes_button.setText("Notes: On" if self._notes_mode else "Notes: Off")

    def _on_pad_digit_clicked(self, digit):
        if self._ended or self._selected is None:
            return
        if self._notes_mode:
            self._toggle_note(self._selected, digit)
        else:
            self.cells[self._selected].setText(str(digit))

    def _toggle_note(self, pos, digit):
        if self.cells[pos].text():
            return  # already has a real value -- pencil marks don't apply to a filled cell
        marks = self._notes.setdefault(pos, set())
        if digit in marks:
            marks.discard(digit)
        else:
            marks.add(digit)
        self._refresh_notes_display(pos)

    def _on_pad_erase_clicked(self):
        if self._ended or self._selected is None:
            return
        if self._notes_mode:
            self._notes[self._selected] = set()
            self._refresh_notes_display(self._selected)
        else:
            self.cells[self._selected].clear()

    def _on_cell_changed(self, row, col):
        if self._ended:
            return
        pos = (row, col)
        text = self.cells[pos].text()
        self.grid[row][col] = int(text) if text.isdigit() else 0
        if text:
            self._notes[pos] = set()  # a real value makes any pencil marks moot
        self._refresh_notes_display(pos)
        self._check_complete()

    def _check_complete(self):
        if any(0 in row for row in self.grid):
            self.status_label.setText(
                "Fill every row, column, and 3x3 box with 1-9 -- click a cell, then a number below. "
                "Notes toggles pencil-mark candidates instead of filling the cell in."
            )
            return
        if self._is_valid_complete():
            self._ended = True
            self.status_label.setText("Solved!")
            for cell in self.cells.values():
                cell.setReadOnly(True)
            self.game_finished.emit(True)
        else:
            self.status_label.setText("Grid is full but has conflicts -- keep adjusting")

    def _is_valid_complete(self):
        full_set = set(range(1, SIDE + 1))
        for i in range(SIDE):
            if set(self.grid[i]) != full_set:
                return False
            if {self.grid[r][i] for r in range(SIDE)} != full_set:
                return False
        for box_r in range(0, SIDE, BOX):
            for box_c in range(0, SIDE, BOX):
                box = {
                    self.grid[box_r + i][box_c + j]
                    for i in range(BOX) for j in range(BOX)
                }
                if box != full_set:
                    return False
        return True
