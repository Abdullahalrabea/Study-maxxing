#manages the study blocks / breaks timer
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton, QMainWindow
from PyQt6.QtCore import Qt, QTimer, pyqtSignal

STUDY_BLOCK_MAX_SECONDS = 50 * 60      # 50 minutes, the cap on any one study block
BREAK_BETWEEN_BLOCKS_SECONDS = 10 * 60  # 10 minutes -- the mandatory break between consecutive study blocks
DEFAULT_BREAK_SECONDS = BREAK_BETWEEN_BLOCKS_SECONDS  # the plain "Start Break" fallback duration (standalone demo mode)


def make_study_blocks(total_seconds, max_block_seconds=STUDY_BLOCK_MAX_SECONDS):
    """Splits total_seconds of needed study time into a list of study-block
    durations (seconds), each capped at max_block_seconds -- the FINAL
    block is sized to whatever's actually left over, never padded up to
    the cap, so a session needing e.g. 140 minutes at a 50-minute cap
    becomes [50min, 50min, 40min], not three full 50-minute blocks. A
    mandatory BREAK_BETWEEN_BLOCKS_SECONDS break is implied between each
    consecutive pair -- it's not part of this list, see
    StudyTimerWidget.set_total_minutes(), which is what actually uses this.
    total_seconds <= 0 returns an empty list (nothing to study)."""
    if total_seconds <= 0:
        return []
    blocks = []
    remaining = total_seconds
    while remaining > 0:
        block = min(remaining, max_block_seconds)
        blocks.append(block)
        remaining -= block
    return blocks


class StudyTimerWidget(QWidget):
    """The timer's actual UI + state machine, as a plain widget so it can be
    embedded (e.g. as a tab) instead of only ever being its own window."""

    session_starting = pyqtSignal()      # emitted on the first button press, before the countdown actually begins
    session_started = pyqtSignal()       # emitted once begin_study_countdown() actually starts the countdown
    break_choice_needed = pyqtSignal()   # emitted the instant a study block ends AND another one still follows it,
                                          # before any break timer starts -- lets external code (e.g. the mini-game
                                          # break screen) decide what happens next instead of relying on the
                                          # built-in "Start Break" button. Fires once per break needed -- for an
                                          # N-block session that's N-1 times, not just once.
    break_started = pyqtSignal()         # emitted when a break countdown begins
    break_ended = pyqtSignal()           # emitted when a break is over and the next study block starts (whether
                                          # the break actually ran, or was skipped entirely -- see skip_break())
    finished = pyqtSignal()              # emitted once the whole session (every study block) is done

    def __init__(self, parent=None):
        super().__init__(parent)
        self._paused_for_distraction = False
        self._paused_for_lockdown_exit = False

        layout = QVBoxLayout()
        self.setLayout(layout)

        # Big timer display -- kept large but not so large it forces the
        # whole left panel wider than it needs to be (an 80px font made
        # "00:20" alone ~400px wide, wider than the entire panel).
        self.time_label = QLabel("00:20")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_label.setStyleSheet("font-size: 48px; font-weight: bold;")
        layout.addWidget(self.time_label)

        self.start_button = QPushButton("Start Focus Block")
        self.start_button.setStyleSheet("font-size: 16px; padding: 8px;")
        layout.addWidget(self.start_button)

        # Timer brain -- explicit phases, generalized to however many study
        # blocks a session actually needs (see make_study_blocks() /
        # set_total_minutes()), not a fixed two:
        #   not_started --[press]--> loading --[monitor ready]--> study
        #     --[countdown, more blocks left]--> awaiting_break
        #       --[choice]--> break --[countdown]--> study (next block) --> ... (repeats)
        #       --[choice]--> study (next block) directly (skip_break())
        #     --[countdown, that was the LAST block]--> done
        # "loading" exists so the countdown doesn't start ticking before the
        # distraction monitor has actually finished loading the webcam/models.
        # Once awaiting_break is reached, what happens next is normally
        # driven externally (skip_break() / start_break_with_duration()) --
        # see break_choice_needed -- the built-in button below is a plain
        # fallback for standalone use (`python core/state_machine.py`),
        # starting a DEFAULT_BREAK_SECONDS break with no choice involved.
        self.phase = "not_started"      # waiting for the first button press
        self.blocks = [STUDY_BLOCK_MAX_SECONDS]  # this session's study-block durations, in order -- one
                                                  # traditional block unless set_total_minutes() says otherwise
        self.block_index = 0            # which block (0-based) is either active or about to start
        self.time_left = self.blocks[0]
        self.break_left = BREAK_BETWEEN_BLOCKS_SECONDS

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_clock)
        self.start_button.clicked.connect(self.start_clicked)

    def set_total_minutes(self, total_minutes):
        """Configures this session's study blocks from a total amount of
        needed study time, split via make_study_blocks() (max
        STUDY_BLOCK_MAX_SECONDS per block, remainder-sized final block, a
        mandatory break between each consecutive pair). Must be called
        before the first Start press (while still not_started) -- a no-op
        once a session is already running, since the block sequence isn't
        meant to change mid-session."""
        if self.phase != "not_started":
            return
        seconds = max(1, round(total_minutes * 60))
        self.blocks = make_study_blocks(seconds) or [STUDY_BLOCK_MAX_SECONDS]
        self.block_index = 0
        self._update_display(self.blocks[0])

    def start_clicked(self):
        """Handles every button press: 1st press kicks off loading (the
        countdown itself only starts once begin_study_countdown() is called);
        2nd press is the plain fallback path for awaiting_break -- starts a
        default-length break directly, with no choice screen. The real app
        instead hides this button during awaiting_break and drives the
        transition via skip_break() / start_break_with_duration()."""
        if self.phase == "not_started":
            self.phase = "loading"
            self.start_button.setEnabled(False)
            self.start_button.setText("Loading webcam...")
            self.session_starting.emit()
        elif self.phase == "awaiting_break":
            self.start_break_with_duration(self.break_left)

    def begin_study_countdown(self):
        """Called once the distraction monitor confirms it's fully loaded
        (webcam + models ready) -- this is when the first study block's
        countdown actually starts ticking. A no-op if called outside the
        loading phase (e.g. a late/duplicate ready signal)."""
        if self.phase != "loading":
            return
        self.phase = "study"
        self.start_button.setText("Studying has started...")
        self.timer.start(1000)
        self.session_started.emit()

    def cancel_loading(self):
        """Called if the distraction monitor fails to load -- reverts to
        not_started so the user can retry, instead of getting stuck waiting
        forever in the loading phase."""
        if self.phase == "loading":
            self.phase = "not_started"
            self.start_button.setEnabled(True)
            self.start_button.setText("Start Focus Block")

    def _update_display(self, seconds: int):
        """Set time_left and refresh the label, without touching the timer."""
        self.time_left = seconds
        minutes = seconds // 60
        secs = seconds % 60
        self.time_label.setText(f"{minutes:02d}:{secs:02d}")

    def _restart_timer_with(self, seconds: int):
        """Stop current timer, set new remaining time, update label, and restart."""
        self.timer.stop()
        self._update_display(seconds)
        self.timer.start(1000)

    def _is_last_block(self):
        return self.block_index >= len(self.blocks) - 1

    def _advance_to_next_block(self):
        """Moves from a finished/skipped break to the next study block --
        shared by update_clock()'s natural break-timeout path and
        skip_break()'s on-demand path, since both reach the exact same
        next state."""
        self.block_index += 1
        self.phase = "study"
        self._restart_timer_with(self.blocks[self.block_index])
        self.start_button.hide()
        self.break_ended.emit()

    def update_clock(self):
        """Runs every second; handles countdown and phase transitions."""
        self.time_left -= 1
        minutes = self.time_left // 60
        secs = self.time_left % 60
        self.time_label.setText(f"{minutes:02d}:{secs:02d}")

        if self.time_left > 0:
            return

        if self.phase == "study":
            self.timer.stop()
            if self._is_last_block():
                self.phase = "done"
                self.time_label.setText("Done!")
                self.start_button.hide()
                self.finished.emit()
            else:
                self.phase = "awaiting_break"
                self._update_display(self.break_left)
                self.start_button.setEnabled(True)
                self.start_button.setText("Start Break")
                self.break_choice_needed.emit()
        elif self.phase == "break":
            self._advance_to_next_block()

    def finish_block_early(self):
        """Force-completes the CURRENT study block right now, as if its
        timer had naturally reached zero -- e.g. Monitor Lockdown
        detecting the material itself has hit 100% completion well
        before the allotted time runs out, so there's no reason to keep
        sitting through unused time. Only valid during "study" phase (a
        no-op otherwise -- e.g. already on a break, or already done).
        Reuses update_clock()'s exact same zero-crossing logic, so an
        early finish and a natural one always reach the identical next
        state (done, or awaiting_break)."""
        if self.phase != "study":
            return
        self.time_left = 0
        self.update_clock()

    def skip_break(self):
        """Moves straight to the next study block, skipping the rest of the
        break -- from awaiting_break (BreakChoiceWindow's own "Skip"
        choice, or a flexible Pocket window that expired unused, both
        before any game was actually chosen) OR from break (GameHostWindow's
        "Skip Break" button, clicked while a game is actively being played
        after Solve/Pocket already committed to one) -- same end state
        either way, just reached from a different point in the break."""
        if self.phase not in ("awaiting_break", "break"):
            return
        self._advance_to_next_block()

    def start_break_with_duration(self, seconds):
        """Starts the break countdown for a given duration (e.g. a strict
        10-minute mini-game session) -- only valid from awaiting_break."""
        if self.phase != "awaiting_break":
            return
        self.phase = "break"
        self.start_button.setEnabled(False)
        self.start_button.setText("Break time running...")
        self.break_started.emit()
        self._restart_timer_with(seconds)

    def pause_for_distraction(self):
        """Called when the distraction monitor enters a pose-challenge/
        push-ups state -- stops the countdown if it's currently running, to
        be resumed via resume_after_distraction() once that clears. A no-op
        during phases where the timer isn't ticking anyway (not_started,
        awaiting_break, done)."""
        if self.timer.isActive():
            self.timer.stop()
            self._paused_for_distraction = True

    def resume_after_distraction(self):
        """Restarts the countdown, but only if pause_for_distraction() was
        the one that stopped it."""
        if self._paused_for_distraction:
            self._paused_for_distraction = False
            self.timer.start(1000)

    def total_study_seconds(self):
        """Total STUDY time (excludes breaks) this whole session is
        configured for -- e.g. for a lockdown pacing progress bar that
        needs to compare elapsed study time against the total, not just
        the current block's slice of it."""
        return sum(self.blocks)

    def study_seconds_elapsed(self):
        """Total STUDY time actually elapsed so far across the whole
        session (excludes break time -- a break isn't "falling behind").
        not_started/loading -> 0; mid-study -> completed blocks plus
        however far into the current one; awaiting_break/break -> every
        block through the one just finished (the next one hasn't started
        ticking yet); done -> the full total."""
        if self.phase in ("not_started", "loading"):
            return 0
        if self.phase in ("awaiting_break", "break"):
            return sum(self.blocks[:self.block_index + 1])
        if self.phase == "done":
            return sum(self.blocks)
        return sum(self.blocks[:self.block_index]) + (self.blocks[self.block_index] - self.time_left)

    def pause_for_lockdown_exit(self):
        """Called when lockdown is exited (Esc/button) while a study or
        break block is actively counting down -- stops the countdown so
        time isn't silently spent unmonitored, to be resumed via
        resume_after_lockdown_exit() once lockdown reopens. Returns True if
        it actually paused something (i.e. there's now something to
        resume), False if the timer wasn't running anyway (e.g. exited
        during awaiting_break, or the session had already finished)."""
        if self.timer.isActive():
            self.timer.stop()
            self._paused_for_lockdown_exit = True
            return True
        return False

    def resume_after_lockdown_exit(self):
        """Restarts the countdown, but only if pause_for_lockdown_exit() was
        the one that stopped it."""
        if self._paused_for_lockdown_exit:
            self._paused_for_lockdown_exit = False
            self.timer.start(1000)


class StudyWarden(QMainWindow):
    """Thin standalone-window wrapper around StudyTimerWidget, kept so
    `python core/state_machine.py` still works on its own."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Study Warden")
        self.resize(400, 300)
        self.setCentralWidget(StudyTimerWidget())


if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    window = StudyWarden()
    window.show()
    sys.exit(app.exec())
