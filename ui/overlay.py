# During a break, the user picks how to spend it -- three windows
# implement that choice and everything that follows it:
#
#   BreakChoiceWindow  -- Solve / Skip / Pocket, shown full-screen the
#                         instant StudyTimerWidget.break_choice_needed
#                         fires.
#   PocketWaitingWindow -- Pocket's flexible "must launch or forfeit"
#                         countdown. A small FLOATING control, not full-
#                         screen -- closing BreakChoiceWindow behind it
#                         reveals the still-open lockdown view (today's
#                         study material) underneath, so choosing Pocket
#                         goes back to the slides instead of a blocking
#                         waiting screen.
#   GameHostWindow      -- the actual mini-game library, full-screen.
#                         Picks a random game, swaps in a new random one
#                         (never repeating the last) whenever the current
#                         one finishes, and keeps doing that until closed.
#
# The strict 10-minute timer that governs a game session is NOT owned by
# this module -- it's StudyTimerWidget.start_break_with_duration()'s
# existing break1 countdown (see core/state_machine.py). BreakFlowController
# just starts that at the right moment and closes GameHostWindow when
# break_ended fires because it ran out. The only timer genuinely owned here
# is Pocket's flexible pre-launch window, which the state machine has no
# concept of (it's still sitting in awaiting_break the whole time).

import random

from PyQt6.QtCore import Qt, QObject, QTimer, QPropertyAnimation, QEasingCurve, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGraphicsOpacityEffect, QDialog, QMenu,
)

from snake_game import SnakeGame
from slide_puzzle_game import SlidePuzzleGame
from memory_match_game import MemoryMatchGame
from minesweeper_game import MinesweeperGame
from sudoku_game import SudokuGame
import theme
import game_theme
import lockdown_os

GAME_CLASSES = [SnakeGame, SlidePuzzleGame, MemoryMatchGame, MinesweeperGame, SudokuGame]
GAME_SECONDS = 10 * 60            # the strict, locked game-playing timer -- Solve, and Pocket once launched
POCKET_WINDOW_SECONDS = 10 * 60   # Pocket's flexible "launch before this runs out" window
FADE_MS = 300
POCKET_FLOAT_WIDTH = 300
POCKET_FLOAT_HEIGHT = 160
POCKET_FLOAT_MARGIN = 24
POCKET_MONITOR = 0  # same monitor LockdownWindow itself uses -- floats over the visible study material there


def _pick_random_game(exclude_class=None):
    """Picks a random game, avoiding an immediate repeat of exclude_class
    when there's another option -- keeps consecutive games varied instead
    of occasionally handing back the same one twice in a row."""
    choices = [g for g in GAME_CLASSES if g is not exclude_class] or GAME_CLASSES
    return random.choice(choices)


def _fade_in_fullscreen(window):
    fade_effect = QGraphicsOpacityEffect(window)
    window.setGraphicsEffect(fade_effect)
    window.setWindowFlags(window.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    window.showFullScreen()
    animation = QPropertyAnimation(fade_effect, b"opacity", window)
    animation.setDuration(FADE_MS)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setEasingCurve(QEasingCurve.Type.OutCubic)
    animation.finished.connect(lambda: window.setGraphicsEffect(None))
    animation.start()
    window._entrance_animation = animation  # keep a live reference -- Qt drops unowned QPropertyAnimations mid-flight

    # Belt-and-suspenders against a real, observed bug: a freshly-opened
    # window here has ended up minimized shortly after opening (most
    # reliably via the Pocket flow, where a sibling window is fading out/
    # closing at nearly the same moment this one opens) even though its
    # title is allow-listed against LockdownEnforcer's periodic minimize
    # sweep -- some transient race around that concurrent transition, not
    # conclusively pinned down from reading the enforcement code alone.
    # Re-asserting full-screen/active state once the dust has settled
    # self-heals it regardless of the exact underlying cause.
    QTimer.singleShot(FADE_MS + 100, lambda: _reassert_fullscreen(window))


def _reassert_fullscreen(window):
    try:
        if window.isMinimized():
            window.showFullScreen()
        window.raise_()
        window.activateWindow()
    except RuntimeError:
        pass  # the window was already closed (e.g. a fast Skip/Exit click) before this fired


def _fade_in_floating(window, width, height, margin=POCKET_FLOAT_MARGIN):
    """Like _fade_in_fullscreen, but for a small floating control meant to
    sit ON TOP of whatever's already showing rather than take over the
    whole screen -- e.g. Pocket's waiting window, which should leave the
    lockdown view (today's study material) visible underneath/around it
    instead of blocking it with a dedicated full-screen "waiting" screen."""
    window.setWindowFlags(window.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    geometry = lockdown_os.screen_geometry(POCKET_MONITOR)
    if geometry is not None:
        x = geometry.x() + geometry.width() - width - margin
        y = geometry.y() + margin
    else:
        x, y = margin, margin
    window.setGeometry(x, y, width, height)

    fade_effect = QGraphicsOpacityEffect(window)
    window.setGraphicsEffect(fade_effect)
    window.show()
    animation = QPropertyAnimation(fade_effect, b"opacity", window)
    animation.setDuration(FADE_MS)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setEasingCurve(QEasingCurve.Type.OutCubic)
    animation.finished.connect(lambda: window.setGraphicsEffect(None))
    animation.start()
    window._entrance_animation = animation

    # Same belt-and-suspenders reasoning as _fade_in_fullscreen's -- just
    # restoring "shown, on top, active" rather than full-screen specifically.
    QTimer.singleShot(FADE_MS + 100, lambda: _reassert_shown(window))


def _reassert_shown(window):
    try:
        if window.isMinimized():
            window.showNormal()
        window.raise_()
        window.activateWindow()
    except RuntimeError:
        pass  # the window was already closed before this fired


def _fade_out_and_close(window):
    fade_effect = QGraphicsOpacityEffect(window)
    window.setGraphicsEffect(fade_effect)
    animation = QPropertyAnimation(fade_effect, b"opacity", window)
    animation.setDuration(FADE_MS)
    animation.setStartValue(1.0)
    animation.setEndValue(0.0)
    animation.setEasingCurve(QEasingCurve.Type.OutCubic)
    animation.finished.connect(window.close)
    animation.start()
    window._exit_animation = animation


class BreakChoiceWindow(QWidget):
    """The three-way break choice screen. Emits exactly one of its three
    signals, once, in response to a button click -- nothing else closes
    this window or picks a default."""

    solve_chosen = pyqtSignal()
    skip_chosen = pyqtSignal()
    pocket_chosen = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Study Warden -- Break")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("Break time -- what do you want to do?")
        title.setStyleSheet("font-size: 26px; font-weight: bold;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(title)

        row = QHBoxLayout()
        row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self._make_choice_column(
            "Solve", "Lock into a random mini-game for a strict 10-minute timer.", self.solve_chosen
        ))
        row.addWidget(self._make_choice_column(
            "Skip", "Skip the break entirely -- start the second 50-minute block right now.", self.skip_chosen
        ))
        row.addWidget(self._make_choice_column(
            "Pocket",
            "Flexible 10-minute window -- launch the game any time before it runs out, "
            "or the break is forfeited. Launching starts its own strict 10-minute timer.",
            self.pocket_chosen,
        ))
        outer.addLayout(row)

    def _make_choice_column(self, title, description, signal):
        container = QWidget()
        container.setFixedWidth(240)
        layout = QVBoxLayout(container)

        button = QPushButton(title)
        button.setStyleSheet("font-size: 18px; font-weight: bold; padding: 16px;")
        button.clicked.connect(signal.emit)
        layout.addWidget(button)

        description_label = QLabel(description)
        description_label.setWordWrap(True)
        description_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(description_label)

        return container

    def show_animated(self):
        _fade_in_fullscreen(self)

    def close_animated(self):
        _fade_out_and_close(self)


class PocketWaitingWindow(QWidget):
    """Pocket's flexible pre-launch window: counts down from
    POCKET_WINDOW_SECONDS, offering a "Play now" button the whole time.
    Emits launch_requested if that's clicked in time, or expired if the
    countdown reaches zero first -- exactly one of the two, once.

    A small floating control, NOT full-screen -- choosing Pocket goes back
    to the lockdown view (today's study material) underneath it, so you
    can keep reviewing it while deciding when to launch, rather than being
    stuck on a dedicated screen with nothing else to do until you decide."""

    launch_requested = pyqtSignal()
    expired = pyqtSignal()

    def __init__(self, seconds=POCKET_WINDOW_SECONDS, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Study Warden -- Pocket Break")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._seconds_left = seconds
        self._ended = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        self.title_label = QLabel("Pocket -- launch before time runs out, or it's forfeited")
        self.title_label.setWordWrap(True)
        outer.addWidget(self.title_label)

        self.time_label = QLabel()
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.time_label)
        self._refresh_label()

        self.play_button = QPushButton("Play now")
        self.play_button.clicked.connect(self._on_play_clicked)
        outer.addWidget(self.play_button)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

        self._apply_theme_colors(theme.manager.colors)
        theme.manager.changed.connect(self._on_theme_changed)

    def _on_theme_changed(self, theme_id, colors):
        self._apply_theme_colors(colors)

    def _apply_theme_colors(self, colors):
        self.setStyleSheet(game_theme.panel_style(colors, radius=10))
        self.title_label.setStyleSheet(game_theme.status_style(colors, size=13))
        self.time_label.setStyleSheet(game_theme.heading_style(colors, size=30))
        self.play_button.setStyleSheet(game_theme.primary_button_style(colors, size=14))

    def _refresh_label(self):
        minutes, seconds = divmod(max(self._seconds_left, 0), 60)
        self.time_label.setText(f"{minutes:02d}:{seconds:02d}")

    def _tick(self):
        if self._ended:
            return
        self._seconds_left -= 1
        self._refresh_label()
        if self._seconds_left <= 0:
            self._end(expired=True)

    def _on_play_clicked(self):
        if self._ended:
            return
        self._end(expired=False)

    def _end(self, expired):
        self._ended = True
        self._timer.stop()
        if expired:
            self.expired.emit()
        else:
            self.launch_requested.emit()

    def show_animated(self):
        _fade_in_floating(self, POCKET_FLOAT_WIDTH, POCKET_FLOAT_HEIGHT)

    def close_animated(self):
        _fade_out_and_close(self)


class GameHostWindow(QWidget):
    """Full-screen host for the mini-game library. Picks a random game on
    construction; whenever the current one finishes (win or lose), swaps in
    a fresh randomly-picked game so play never just stops on its own --
    only closing this window (normally driven by break_ended, once the
    strict timer that's actually running this break expires) ends the
    session.

    The countdown shown here is a purely visual, LOCAL timer seeded from
    `seconds` at construction time -- it is not what actually ends the
    break. The real, authoritative countdown is StudyTimerWidget's own
    break1 timer (see core/state_machine.py); BreakFlowController closes
    this window via break_ended once THAT reaches zero. Both always agree
    in practice since this window is always created with GAME_SECONDS at
    the same moment start_break_with_duration(GAME_SECONDS) is called."""

    # Emitted by the "Skip Break" button -- BreakFlowController listens for
    # this and treats it exactly like BreakChoiceWindow's own Skip button
    # (ends the break right now, starts the second study block). Not
    # handled internally here since closing this window and driving the
    # timer are both BreakFlowController's job, not this window's own.
    skip_to_study_requested = pyqtSignal()

    def __init__(self, seconds=GAME_SECONDS, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Study Warden -- Break")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._seconds_left = seconds

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(12)

        self.time_label = QLabel()
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._refresh_time_label()
        # Tucked away behind a small "hover to show timer" hint until
        # hovered, same as the real study timer (see theme.HoverRevealPanel)
        # -- big countdowns recede when you're not actually checking them.
        outer.addWidget(theme.HoverRevealPanel(self.time_label))

        self.game_name_label = QLabel()
        self.game_name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.game_name_label)

        # Two different scopes of "skip", side by side: Skip Game discards
        # only the current game (the strict timer keeps running -- same
        # swap a win/loss already does); Skip Break ends the break itself
        # early and starts the second study block right now, same as
        # BreakChoiceWindow's own Skip button -- styled as the visually
        # "bigger" action (primary_button_style) since it's the bigger
        # decision of the two.
        skip_row = QHBoxLayout()
        skip_row.addStretch()
        self.skip_game_button = QPushButton("Skip Game")
        self.skip_game_button.setToolTip("Discard this game and start a different random one -- the break timer keeps running.")
        self.skip_game_button.clicked.connect(self._on_skip_game_clicked)
        skip_row.addWidget(self.skip_game_button)
        self.skip_break_button = QPushButton("Skip Break -- Start Studying")
        self.skip_break_button.setToolTip("Ends the break right now and starts the second study block.")
        self.skip_break_button.clicked.connect(self.skip_to_study_requested.emit)
        skip_row.addWidget(self.skip_break_button)
        skip_row.addStretch()
        outer.addLayout(skip_row)

        self._game_container = QVBoxLayout()
        self._game_container.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addLayout(self._game_container, 1)

        self._current_game = None
        self._start_new_game()

        self._apply_theme_colors(theme.manager.colors)
        theme.manager.changed.connect(self._on_theme_changed)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

    def _on_theme_changed(self, theme_id, colors):
        self._apply_theme_colors(colors)

    def _apply_theme_colors(self, colors):
        self.setStyleSheet(f"background-color: {colors['bg']};")
        self.time_label.setStyleSheet(game_theme.heading_style(colors, size=34))
        self.game_name_label.setStyleSheet(game_theme.heading_style(colors, size=20))
        self.skip_game_button.setStyleSheet(game_theme.button_style(colors, size=14))
        self.skip_break_button.setStyleSheet(game_theme.primary_button_style(colors, size=14))

    def _refresh_time_label(self):
        minutes, seconds = divmod(max(self._seconds_left, 0), 60)
        self.time_label.setText(f"{minutes:02d}:{seconds:02d}")

    def _tick(self):
        self._seconds_left -= 1
        self._refresh_time_label()
        if self._seconds_left <= 0:
            self._timer.stop()  # the real close comes from break_ended, not this

    def _start_new_game(self):
        previous_class = type(self._current_game) if self._current_game is not None else None
        if self._current_game is not None:
            self._game_container.removeWidget(self._current_game)
            # hide() immediately -- deleteLater()'s actual deletion is
            # deferred to the next event-loop pass, which would otherwise
            # leave the old (now unmanaged-by-layout) widget visible,
            # sitting at its last position, until then.
            self._current_game.hide()
            self._current_game.deleteLater()

        game_class = _pick_random_game(exclude_class=previous_class)
        self._current_game = game_class(self)
        self._current_game.game_finished.connect(self._on_game_finished)
        self.game_name_label.setText(game_class.NAME)
        self._game_container.addWidget(self._current_game, 0, Qt.AlignmentFlag.AlignCenter)
        self._current_game.setFocus()

    def _on_game_finished(self, won):
        # No distinction between a win and a loss here -- "if i finish the
        # game before the timer finishes start a new game" applies either way.
        self._start_new_game()

    def _on_skip_game_clicked(self):
        self._start_new_game()

    def show_animated(self):
        _fade_in_fullscreen(self)

    def close_animated(self):
        _fade_out_and_close(self)


class GamePreviewDialog(QDialog):
    """A plain, no-strings-attached way to try ONE mini-game outside the
    real break flow -- launched from the homepage's own "Mini-Games"
    menu (see build_game_picker_menu() below), purely so you can see
    what a break's game selection looks/plays like without needing a
    real study session running. No strict timer, no forced swap when
    it finishes, no Skip Break -- just the game and a Close button,
    closes whenever you want. GameHostWindow (above) is the real,
    break-timer-driven version this deliberately does NOT try to be."""

    def __init__(self, game_class, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Study Warden -- {game_class.NAME} (preview)")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        self.game = game_class(parent=self)
        layout.addWidget(self.game)

        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        layout.addWidget(self.close_button, alignment=Qt.AlignmentFlag.AlignCenter)

        self._apply_theme(theme.manager.colors)
        theme.manager.changed.connect(self._on_theme_changed)

    def _on_theme_changed(self, theme_id, colors):
        self._apply_theme(colors)

    def _apply_theme(self, colors):
        self.setStyleSheet(f"background-color: {colors['bg']};")
        self.close_button.setStyleSheet(game_theme.button_style(colors, size=14))


def build_game_picker_menu(parent):
    """A QMenu listing every game in GAME_CLASSES by name -- picking one
    opens it in a GamePreviewDialog. Built fresh each time it's shown
    (see its caller in main_window.py) rather than kept around, so it's
    always current if GAME_CLASSES ever changes; QMenu construction is
    cheap enough that this isn't worth caching."""
    menu = QMenu("Mini-Games", parent)
    for game_class in GAME_CLASSES:
        action = menu.addAction(game_class.NAME)
        action.triggered.connect(lambda _checked, gc=game_class: GamePreviewDialog(gc, parent).exec())
    return menu


class BreakFlowController(QObject):
    """Wires StudyTimerWidget's break_choice_needed into the Solve/Skip/
    Pocket screen and owns whichever full-screen window that choice leads
    to, so FocusSessionWidget only has to construct this once and never
    touch the break-choice/game internals directly."""

    def __init__(self, timer_widget, parent=None):
        super().__init__(parent)
        self.timer_widget = timer_widget
        self._active_window = None
        timer_widget.break_choice_needed.connect(self._on_break_choice_needed)
        timer_widget.break_ended.connect(self._on_break_ended)

    def _close_active(self):
        if self._active_window is not None:
            window = self._active_window
            self._active_window = None
            window.close_animated()

    def _on_break_choice_needed(self):
        window = BreakChoiceWindow()
        window.solve_chosen.connect(self._on_solve_chosen)
        window.skip_chosen.connect(self._on_skip_chosen)
        window.pocket_chosen.connect(self._on_pocket_chosen)
        self._active_window = window
        window.show_animated()

    def _on_skip_chosen(self):
        self._close_active()
        self.timer_widget.skip_break()

    def _on_solve_chosen(self):
        self._close_active()
        self._show_game_host()
        # The strict timer only starts now, because Solve was actually
        # picked -- not before, and not for Skip/Pocket at all.
        self.timer_widget.start_break_with_duration(GAME_SECONDS)

    def _on_pocket_chosen(self):
        self._close_active()
        window = PocketWaitingWindow()
        window.launch_requested.connect(self._on_pocket_launched)
        window.expired.connect(self._on_pocket_expired)
        self._active_window = window
        window.show_animated()

    def _on_pocket_expired(self):
        self._close_active()
        self.timer_widget.skip_break()

    def _on_pocket_launched(self):
        self._close_active()
        self._show_game_host()
        self.timer_widget.start_break_with_duration(GAME_SECONDS)

    def _show_game_host(self):
        window = GameHostWindow()
        window.skip_to_study_requested.connect(self._on_skip_break_from_game)
        self._active_window = window
        window.show_animated()

    def _on_skip_break_from_game(self):
        # Same effect as BreakChoiceWindow's own Skip button -- close
        # whichever window is up (the game host, in this case) and end the
        # break right now.
        self._close_active()
        self.timer_widget.skip_break()

    def _on_break_ended(self):
        # Fires both when a strict game timer (Solve, or a launched Pocket)
        # actually runs out, and when skip_break() itself was the trigger --
        # in the latter case _active_window is already None from the
        # _close_active() call right before skip_break(), so this is a no-op.
        self._close_active()
