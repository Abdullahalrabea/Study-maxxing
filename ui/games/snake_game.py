# Classic Snake: arrow keys steer, eating food grows the snake, hitting a
# wall or yourself ends the round. Custom-painted on a QTimer game loop
# rather than a widget grid, since it needs smooth, continuous movement.
#
# The canvas is a separate inner widget (_SnakeCanvas), not SnakeGame
# itself -- SnakeGame gets the shared themed panel shell like every other
# game (see base_game.MiniGameWidget), and the canvas's paintEvent fills
# its ENTIRE rect every frame, which would otherwise paint straight over
# (hide) that panel's QSS-drawn border if they were the same widget.
# Keyboard focus still lands on SnakeGame itself (GameHostWindow calls
# .setFocus() on the outer widget), independent of this inner structure.

import random

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPainter, QColor, QFont
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel

from base_game import MiniGameWidget
import theme
import game_theme

GRID_SIZE = 18          # cells per side
CELL_PX = 22
TICK_MS = 140            # game speed -- lower is faster
FOOD_ALERT_HEX = "#FF5555"  # fixed -- "this is food" stays a universal warm color regardless of theme


class _SnakeCanvas(QWidget):
    def __init__(self, game, parent=None):
        super().__init__(parent)
        self.game = game
        self.setFixedSize(GRID_SIZE * CELL_PX, GRID_SIZE * CELL_PX)

    def paintEvent(self, event):
        self.game._paint(self)


class SnakeGame(MiniGameWidget):
    NAME = "Snake"
    game_finished = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._colors = theme.manager.colors

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        self.status_label = QLabel("Arrow keys or WASD to steer -- eat the food, don't hit a wall or yourself")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        self.board_frame = QWidget()
        self.board_frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        board_layout = QVBoxLayout(self.board_frame)
        board_layout.setContentsMargins(8, 8, 8, 8)
        self.canvas = _SnakeCanvas(self)
        board_layout.addWidget(self.canvas)
        outer.addWidget(self.board_frame, 0, Qt.AlignmentFlag.AlignHCenter)

        mid = GRID_SIZE // 2
        self.snake = [(mid, mid), (mid - 1, mid), (mid - 2, mid)]  # head first
        self.direction = (1, 0)
        self._pending_direction = (1, 0)
        self.food = self._place_food()
        self.score = 0
        self._ended = False

        self.on_theme_changed(theme.manager.colors)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(TICK_MS)

    def on_theme_changed(self, colors):
        self._colors = colors
        self.board_frame.setStyleSheet(game_theme.board_frame_style(colors))
        self.status_label.setStyleSheet(game_theme.status_style(colors, size=14))
        self.canvas.update()

    def _place_food(self):
        empty = [
            (x, y) for x in range(GRID_SIZE) for y in range(GRID_SIZE)
            if (x, y) not in self.snake
        ]
        return random.choice(empty) if empty else None

    def keyPressEvent(self, event):
        key = event.key()
        opposite = (-self.direction[0], -self.direction[1])
        new_direction = {
            Qt.Key.Key_Up: (0, -1), Qt.Key.Key_W: (0, -1),
            Qt.Key.Key_Down: (0, 1), Qt.Key.Key_S: (0, 1),
            Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_A: (-1, 0),
            Qt.Key.Key_Right: (1, 0), Qt.Key.Key_D: (1, 0),
        }.get(key)
        if new_direction and new_direction != opposite:
            self._pending_direction = new_direction
        else:
            super().keyPressEvent(event)

    def _tick(self):
        if self._ended:
            return
        self.direction = self._pending_direction
        head_x, head_y = self.snake[0]
        dx, dy = self.direction
        new_head = (head_x + dx, head_y + dy)

        hit_wall = not (0 <= new_head[0] < GRID_SIZE and 0 <= new_head[1] < GRID_SIZE)
        hit_self = new_head in self.snake
        if hit_wall or hit_self:
            self._end(won=self.score >= 10)  # a decent-length snake counts as a "win"
            return

        self.snake.insert(0, new_head)
        if new_head == self.food:
            self.score += 1
            self.food = self._place_food()
            if self.food is None:
                self._end(won=True)  # filled the whole board
                return
        else:
            self.snake.pop()

        self.canvas.update()

    def _end(self, won):
        if self._ended:
            return
        self._ended = True
        self.timer.stop()
        self.status_label.setText("You win!" if won else "Game over")
        self.canvas.update()
        self.game_finished.emit(won)

    def _paint(self, canvas_widget):
        colors = self._colors
        painter = QPainter(canvas_widget)
        painter.fillRect(canvas_widget.rect(), QColor(colors["bg"]))

        body_color = QColor(colors["accent"])
        for x, y in self.snake:
            painter.fillRect(x * CELL_PX + 1, y * CELL_PX + 1, CELL_PX - 2, CELL_PX - 2, body_color)
        head_x, head_y = self.snake[0]
        head_color = QColor(game_theme.light_tint(colors, base_key="accent", weight=0.55))
        painter.fillRect(head_x * CELL_PX + 1, head_y * CELL_PX + 1, CELL_PX - 2, CELL_PX - 2, head_color)

        if self.food is not None:
            fx, fy = self.food
            painter.fillRect(fx * CELL_PX + 4, fy * CELL_PX + 4, CELL_PX - 8, CELL_PX - 8, QColor(FOOD_ALERT_HEX))

        painter.setPen(QColor(colors["accent"]))
        painter.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        painter.drawText(6, 18, f"Score: {self.score}")

        if self._ended:
            painter.fillRect(canvas_widget.rect(), QColor(0, 0, 0, 170))
            painter.setPen(QColor("#FFFFFF"))
            painter.setFont(QFont("Consolas", 16, QFont.Weight.Bold))
            painter.drawText(canvas_widget.rect(), Qt.AlignmentFlag.AlignCenter, "Game Over")
