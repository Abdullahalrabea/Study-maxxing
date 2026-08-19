#PyQt6.Tkinter layout
import os
import time
import threading

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QLineEdit, QSplitter, QScrollArea, QSpinBox, QDialog, QCheckBox
)
from PyQt6.QtCore import QThread, pyqtSignal, QSettings, QUrl, Qt, QTimer

import passive_tracking
from paths import get_app_data_dir
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PyQt6.QtWebEngineWidgets import QWebEngineView

from state_machine import StudyTimerWidget
from exam_form_panel import ExamFormPanel, EXAM_FORM_HELP_TEXT
from study_planner_panel import StudyPlannerPanel, STUDY_PLANNER_HELP_TEXT
from revision_panel import RevisionPanel, REVISION_HELP_TEXT
from engagement_dashboard import EngagementDashboardWidget
from notion_page_client import NotionPageClient
from theme import HeaderBanner, CollapsibleSection, HoverRevealPanel, manager, Card, RADIUS_TAB
from settings_dialog import (
    SettingsDialog, KEY_NOTION_TOKEN, KEY_NOTION_TABLE_BLOCK_ID, KEY_NOTION_TODO_PAGE_ID,
    KEY_NOTION_CALENDAR_DATABASE_ID, KEY_NOTION_EMBED_URL, KEY_LM_STUDIO_URL,
)

# No hardcoded real values here (this file is public source) -- resolved (in
# order of priority) from an env var, then a value saved via the Settings
# dialog, then these empty fallbacks. First run with nothing configured yet
# shows the Notion panel's own "not configured" state until a real token +
# IDs are pasted into Settings. If you want a personal default that survives
# without touching Settings each time, set the matching env var instead of
# editing this file (NOTION_TOKEN, NOTION_TABLE_BLOCK_ID, NOTION_TODO_PAGE_ID,
# NOTION_CALENDAR_DATABASE_ID, NOTION_EMBED_URL).
DEFAULT_NOTION_TOKEN = ""
DEFAULT_NOTION_TABLE_BLOCK_ID = ""
DEFAULT_NOTION_TODO_PAGE_ID = ""
DEFAULT_NOTION_CALENDAR_DATABASE_ID = ""
DEFAULT_NOTION_EMBED_URL = ""

SETTINGS_ORG = "StudyWarden"
SETTINGS_APP = "StudyWarden"

# "How does this page work" text for each page's header help icon (see
# theme.CollapsibleSection's help_text param) -- kept here rather than as an
# inline label inside each page's own widget so the explanation lives next
# to a hover icon instead of eating permanent vertical space.
FOCUS_SESSION_HELP_TEXT = (
    "Enter the total study time you need -- it's automatically split into blocks of at most 50 "
    "minutes each, with a mandatory 10-minute break between consecutive blocks (e.g. 140 minutes "
    "becomes 50/break/50/break/40, not one long block or three padded ones). Pressing Start loads "
    "the distraction monitor first (webcam + models) -- the countdown only begins once it's "
    "actually ready, so the first few seconds aren't left unwatched. Once it's watching, Monitor "
    "Lockdown takes over full-screen: today's study material, the webcam feed, and this timer. If "
    "it catches you looking away or on your phone too long, the countdown pauses until you clear "
    "the pose challenge (or push-ups) it gives you, then resumes on its own. Esc or the Exit "
    "button gets you back to the normal view -- but pauses the countdown too, since lockdown is "
    "what's being enforced; Resume Lockdown reopens it and continues right where it left off. It "
    "stands down during breaks, and shuts off entirely once the session finishes."
)
TODO_LIST_HELP_TEXT = (
    "New items are added at the top of the list. The \"week 1 starts on\" date is in Settings "
    "(the gear icon, top-left). Passive Tracking is a plain start/stop timer for non-exam work -- "
    "no webcam, no distraction monitoring, no penalties -- logged locally on this machine only, "
    "separate from Monitor Lockdown entirely."
)
ENGAGEMENT_DASHBOARD_HELP_TEXT = (
    "A weekly rollup of the distraction data Monitor Lockdown's AI engagement judgment has always "
    "collected during a session, now actually kept somewhere instead of being thrown away once the "
    "session ends. Local only -- no Notion, no LLM call to view this. Courses are sorted worst-"
    "distraction-rate first, so it's easy to see where focus is actually slipping."
)


def _resolve_setting(env_var, settings, settings_key, default):
    return os.environ.get(env_var) or settings.value(settings_key) or default


def _themed_splitter(orientation):
    """A QSplitter whose handle is actually visible/grabbable (the default
    Qt handle is a near-invisible 1px seam) -- a plain color at rest, the
    theme's accent color on hover so it reads as draggable. Stays in sync
    if the theme changes at runtime."""
    splitter = QSplitter(orientation)

    def apply(colors):
        splitter.setStyleSheet(
            f"QSplitter::handle {{ background-color: {colors['border']}; }}"
            f"QSplitter::handle:hover {{ background-color: {colors['accent']}; }}"
        )

    apply(manager.colors)
    manager.changed.connect(lambda _theme_id, colors: apply(colors))
    return splitter

# Persistent storage for the embedded Notion view's login session (cookies,
# local storage) -- without an explicit named profile + storage path, Qt
# WebEngine's default profile isn't guaranteed to survive between app
# restarts, which is why login wasn't sticking.
NOTION_PROFILE_DIR = get_app_data_dir() / ".notion_web_profile"


def _create_notion_profile(parent):
    NOTION_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    profile = QWebEngineProfile("notion", parent)
    profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
    profile.setPersistentStoragePath(str(NOTION_PROFILE_DIR / "storage"))
    profile.setCachePath(str(NOTION_PROFILE_DIR / "cache"))
    return profile


class PopupWebView(QWebEngineView):
    """A separate top-level window for login popups (Google/Microsoft/Apple/
    SSO sign-in, magic-link confirmation, etc.) that Notion opens via
    window.open()/target="_blank" -- without somewhere for these to go, a
    plain QWebEngineView just silently drops them, which is why login can
    otherwise seem to do nothing when you click it. Shares the main view's
    profile so cookies set here (e.g. completing an OAuth login) count
    toward the same persistent session."""

    def __init__(self, profile):
        super().__init__()
        self.setWindowTitle("Sign in")
        self.resize(500, 700)
        self.setPage(QWebEnginePage(profile, self))


class NotionWebPage(QWebEnginePage):
    """Custom page for the embedded Notion view so popup windows actually
    open (in a real separate window, sharing the same persistent profile)
    instead of being silently dropped."""

    def __init__(self, profile, parent=None):
        super().__init__(profile, parent)
        self._profile = profile
        self._popups = []  # keeps popups alive -- Qt/PyQt won't hold a Python reference otherwise

    def createWindow(self, _window_type):
        popup = PopupWebView(self._profile)
        popup.page().windowCloseRequested.connect(popup.close)
        popup.show()
        self._popups.append(popup)
        return popup.page()


class _OneShotFlag:
    """A threading.Event that clears itself the moment it's consumed --
    for one-time cross-thread signals like "the second study block just
    started, reset your distraction counters", which should fire once
    rather than on every subsequent frame."""

    def __init__(self):
        self._event = threading.Event()

    def request(self):
        self._event.set()

    def consume(self):
        if self._event.is_set():
            self._event.clear()
            return True
        return False


class VisionMonitorThread(QThread):
    """Runs vision/gaze_engine.py's OpenCV monitoring loop off the Qt thread
    so it doesn't freeze the rest of the app. gaze_engine owns its own
    while-loop and cv2 windows entirely; this just gives it a thread to run
    on, plus: a cooperative way to stop it (request_stop()), a signal for
    when the webcam/models have actually finished loading (ready), being
    notified whenever a pose-challenge/push-ups state starts or ends
    (disruption_changed), whether push-ups are currently owed and how many
    are left (penalty_pending_changed, fires continuously while owed so the
    rep count stays live -- see gaze_engine.main()), and a way to suppress
    distraction detection entirely (is_suppressed, e.g. during a break).
    initial_pushups_owed: passed straight through to gaze_engine.main() --
    > 0 starts the loop directly in a push-ups-owed state instead of
    MONITORING (see PenaltyCompletionDialog, a standalone "pay off what's
    owed" flow, not a real study session)."""
    ready = pyqtSignal()                   # webcam + models loaded, first frame captured
    disruption_changed = pyqtSignal(bool)  # True while a pose challenge/push-ups is active
    penalty_pending_changed = pyqtSignal(bool, int, int)  # (pending, reps_done, reps_needed)
    failed = pyqtSignal(str)
    stopped = pyqtSignal()
    # (main_frame_bgr, pose_frame_bgr_or_None) -- raw numpy arrays, one per processed frame,
    # for embedding the feed in a Qt widget (Monitor Lockdown) instead of gaze_engine's own
    # native OpenCV windows.
    frame_ready = pyqtSignal(object, object)

    def __init__(self, is_suppressed=None, on_reset_requested=None, initial_pushups_owed=0):
        super().__init__()
        self._stop_event = threading.Event()
        self._is_suppressed = is_suppressed
        self._on_reset_requested = on_reset_requested
        self._initial_pushups_owed = initial_pushups_owed

    def request_stop(self):
        self._stop_event.set()

    def run(self):
        try:
            import gaze_engine
            gaze_engine.main(
                should_stop=self._stop_event.is_set,
                on_disruption_change=self.disruption_changed.emit,
                on_ready=self.ready.emit,
                is_suppressed=self._is_suppressed,
                on_reset_requested=self._on_reset_requested,
                on_frame=self.frame_ready.emit,
                on_penalty_pending_change=self.penalty_pending_changed.emit,
                initial_pushups_owed=self._initial_pushups_owed,
            )
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            self.stopped.emit()


class MarkCompletedWorker(QThread):
    """Runs one StudyPlanner.mark_session_completed() call off the GUI
    thread -- see FocusSessionWidget._on_material_completed(). A real
    Notion API call (archives the matching calendar entry), so it
    shouldn't block the UI even though the study session itself has
    already moved on by the time this runs."""
    done = pyqtSignal(bool)  # whether a matching entry was actually found and archived
    failed = pyqtSignal(str)

    def __init__(self, planner, course, field, on_date):
        super().__init__()
        self.planner = planner
        self.course = course
        self.field = field
        self.on_date = on_date

    def run(self):
        try:
            found = self.planner.mark_session_completed(self.course, self.field, self.on_date)
            self.done.emit(found)
        except Exception as e:
            self.failed.emit(str(e))


class PenaltyCompletionDialog(QDialog):
    """Standalone "pay off what you owe" flow -- NOT a real study
    session, no material/timer involved at all. Launches its own
    VisionMonitorThread with initial_pushups_owed=N, which makes gaze_
    engine.main() start directly in the push-ups-owed state instead of
    normal MONITORING. Closes itself automatically the moment the reps
    are actually completed (penalty_pending_changed fires False) and
    clears the persisted debt -- closing it any OTHER way (the X button,
    Esc) leaves the debt exactly as it was, since this is a soft nudge,
    not a lock: dismissing it doesn't erase what's still owed."""

    def __init__(self, reps_owed, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Study Warden -- Owed Push-ups")
        self.resize(420, 420)
        self._done = False

        layout = QVBoxLayout(self)
        self.status_label = QLabel(f"Complete {reps_owed} push-up(s) left over from an interrupted penalty.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.webcam_label = QLabel()
        self.webcam_label.setFixedSize(320, 240)
        self.webcam_label.setStyleSheet("background-color: #000;")
        self.webcam_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.webcam_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.progress_label = QLabel("Loading webcam...")
        self.progress_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.progress_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.progress_label)

        close_button = QPushButton("Close (debt stays owed)")
        close_button.clicked.connect(self.close)
        layout.addWidget(close_button)

        self._thread = VisionMonitorThread(initial_pushups_owed=reps_owed)
        self._thread.frame_ready.connect(self._on_frame_ready)
        self._thread.penalty_pending_changed.connect(self._on_penalty_pending_changed)
        self._thread.failed.connect(self._on_failed)
        self._thread.start()

    def _on_frame_ready(self, main_frame, pose_frame):
        from lockdown_window import _bgr_to_pixmap
        pixmap = _bgr_to_pixmap(main_frame)
        self.webcam_label.setPixmap(pixmap.scaled(
            self.webcam_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation
        ))

    def _on_penalty_pending_changed(self, pending, reps_done, reps_needed):
        self.progress_label.setText(f"Push-ups: {reps_done} / {reps_needed}")
        if not pending and reps_done > 0:
            self._done = True
            import session_snapshot
            session_snapshot.clear_penalty()
            self.status_label.setText("Done! Debt cleared.")
            self._thread.request_stop()
            self.close()

    def _on_failed(self, message):
        self.progress_label.setText(f"Couldn't start the monitor: {message}")

    def closeEvent(self, event):
        if self._thread is not None and self._thread.isRunning():
            self._thread.request_stop()
            self._thread.wait(2000)
        super().closeEvent(event)


class FocusSessionWidget(QWidget):
    """Combines the focus timer with the distraction monitor into one
    session: starting a focus block automatically starts the webcam
    monitor; a pose-challenge or push-ups state pauses the countdown until
    it clears; and the timer finishing shuts the monitor down.

    Once the monitor is actually watching, "Monitor Lockdown" takes over
    the screen: today's study material full-screen (auto-picked from
    what's scheduled today + whatever PDF was attached to that plan --
    needs Notion credentials to look up; without them lockdown still opens,
    just with no material found), the live webcam feed top-left, and this
    same timer re-parented up top-center. Esc or the Exit button in that
    view returns to the normal window -- and pauses the countdown, since
    lockdown is what's actually being monitored/enforced; a "Resume
    Lockdown" button reopens it and continues the countdown right where it
    left off -- see ui/lockdown_window.py and
    StudyTimerWidget.pause_for_lockdown_exit().

    The monitor thread itself (webcam + all 4 models) stays running the
    WHOLE session once started -- it's never stopped/reloaded just for a
    break or a lockdown-exit pause, only when the session genuinely ends
    or fails (that reload is a real multi-second cost, not worth paying
    twice for something this routine). Instead it's told to stand down via
    is_suppressed (see _is_monitor_suppressed()) during those two windows
    -- still watching, just not acting on (or accumulating) anything it
    sees, so there's no restart delay either way."""

    def __init__(self, notion_token=None, table_block_id=None, calendar_database_id=None, parent=None):
        super().__init__(parent)
        self._thread = None
        self._break_event = threading.Event()            # set while on break
        self._lockdown_paused_event = threading.Event()   # set while lockdown is exited mid-session
        self._penalty_pending = False   # True while push-ups are currently owed (mirrors gaze_engine's own
                                         # state -- see _on_penalty_pending_changed()); checked at every
                                         # exit point (lockdown close, app close) to decide whether to
                                         # persist a penalty debt for next launch
        self._penalty_reps_done = 0
        self._penalty_reps_needed = 0
        self._reset_flag = _OneShotFlag()       # fires once when the second study block starts
        self._lockdown = None
        self._last_pdf_path = None              # cached so Resume Lockdown can reopen without redoing the material lookup/preview
        self._forced_pdf_path = None            # set by start_custom_session() (e.g. the Revision panel) to
                                                 # override the normal auto-picked-from-schedule material for
                                                 # exactly the next session start, then cleared once consumed
        self._forced_total_slides = 0           # paired with _forced_pdf_path -- the exact slide count the
                                                 # caller's estimate was based on (0 = not given)
        self._current_total_slides = 0          # the slide count LockdownWindow's pacing progress bar uses for
                                                 # THIS session -- persists across Resume Lockdown reopens
                                                 # (unlike _forced_total_slides, which is consumed once); reset
                                                 # to 0 (LockdownWindow falls back to the PDF's real page count)
                                                 # for a normal auto-picked session
        self._current_session_scheduled = None  # the matched {"course", "field", ...} dict (see
                                                 # study_planner_panel.find_todays_material()) when THIS
                                                 # session came from today's calendar -- None for a
                                                 # Revision/forced session, which has nothing on the
                                                 # calendar to snapshot/carry over (see session_snapshot.py)
        self._current_deadline_passed = False   # True if THIS session's course/field deadline has already
                                                 # passed with the material not yet done -- shows a strong,
                                                 # still-escapable warning inside lockdown (see _open_lockdown())

        self._study_planner = None
        if notion_token and table_block_id and calendar_database_id:
            from study_planner import StudyPlanner
            self._study_planner = StudyPlanner(notion_token, table_block_id, calendar_database_id)

        self._layout = QVBoxLayout(self)
        layout = self._layout

        # A soft, persistent nudge (NOT a lock -- dismissible, doesn't
        # block starting a session) shown whenever push-ups are still
        # owed from an interrupted penalty in a previous run. Hidden
        # unless session_snapshot actually has one on record.
        self.penalty_banner = QLabel("")
        self.penalty_banner.setWordWrap(True)
        self.penalty_banner.setStyleSheet(
            f"background-color: #F8D7DA; color: #58151C; padding: 10px; "
            f"border: 1px solid #F1AEB5; border-radius: {RADIUS_TAB}px;"
        )
        self.penalty_banner.setVisible(False)
        layout.addWidget(self.penalty_banner)
        self.penalty_complete_button = QPushButton("Complete Now")
        self.penalty_complete_button.clicked.connect(self._on_penalty_complete_clicked)
        self.penalty_complete_button.setVisible(False)
        layout.addWidget(self.penalty_complete_button)
        self._refresh_penalty_banner()

        # Total study time needed, split automatically into blocks capped
        # at STUDY_BLOCK_MAX_SECONDS with a mandatory break between each
        # consecutive pair (see state_machine.make_study_blocks()) -- e.g.
        # 140 min -> 50/break/50/break/40, not one long uninterrupted
        # block. Defaults to 100 min (two blocks), matching this app's own
        # previous fixed 50/10/50 behavior when nothing else is entered.
        total_minutes_row = QHBoxLayout()
        total_minutes_row.addWidget(QLabel("Total study time needed:"))
        self.total_minutes_spin = QSpinBox()
        self.total_minutes_spin.setRange(10, 600)
        self.total_minutes_spin.setSingleStep(10)
        self.total_minutes_spin.setValue(100)
        self.total_minutes_spin.setSuffix(" min")
        self.total_minutes_spin.valueChanged.connect(self._on_total_minutes_changed)
        total_minutes_row.addWidget(self.total_minutes_spin)
        total_minutes_row.addStretch()
        layout.addLayout(total_minutes_row)

        self.timer_widget = StudyTimerWidget()
        self.timer_widget.set_total_minutes(self.total_minutes_spin.value())
        # The big timer readout stays tucked away behind a small "hover to
        # show timer" hint until hovered (see theme.HoverRevealPanel) --
        # reached in from outside StudyTimerWidget rather than built into
        # it, since core/state_machine.py deliberately doesn't depend on
        # ui/theme.py. Wrapped once, here, so the same behavior travels
        # with the widget whether it's sitting in this panel or re-parented
        # into LockdownWindow later (see _open_lockdown()).
        timer_layout = self.timer_widget.layout()
        label_index = timer_layout.indexOf(self.timer_widget.time_label)
        timer_layout.removeWidget(self.timer_widget.time_label)
        timer_layout.insertWidget(label_index, HoverRevealPanel(self.timer_widget.time_label))

        self.timer_widget.session_starting.connect(self._on_session_starting)
        self.timer_widget.break_started.connect(self._break_event.set)
        self.timer_widget.break_ended.connect(self._break_event.clear)
        self.timer_widget.break_ended.connect(self._reset_flag.request)
        self.timer_widget.finished.connect(self._on_session_finished)
        layout.addWidget(self.timer_widget)

        # Owns the Solve/Skip/Pocket break-choice screen and the mini-game
        # library it can lead to -- see ui/overlay.py. Listens for
        # break_choice_needed itself; nothing else here has to know it exists.
        from overlay import BreakFlowController
        self._break_flow = BreakFlowController(self.timer_widget, parent=self)

        self.monitor_status_label = QLabel("Distraction monitor: not running.")
        layout.addWidget(self.monitor_status_label)

        self.resume_lockdown_button = QPushButton("Resume Lockdown")
        self.resume_lockdown_button.setStyleSheet("font-size: 16px; padding: 8px;")
        self.resume_lockdown_button.clicked.connect(self._on_resume_lockdown_clicked)
        self.resume_lockdown_button.setVisible(False)
        layout.addWidget(self.resume_lockdown_button)

        layout.addStretch()

        # Looked up ONCE here (not lazily inside _begin_lockdown_flow(),
        # where it used to happen) specifically so any outstanding debt
        # for today's course/field can be folded into total_minutes_spin
        # BEFORE the user ever clicks Start -- StudyTimerWidget.
        # set_total_minutes() is a no-op once a session's phase has left
        # "not_started", so this has to happen up front, not once the
        # session's already loading.
        self._todays_material = (None, [], None)  # (pdf_path, topics, session) -- reused by
                                                    # _begin_lockdown_flow() so it sees exactly what was
                                                    # used to compute the debt-adjusted total_minutes below
        if self._study_planner is not None:
            self._check_calendar_rollover()  # move any stale-dated debt's calendar entry forward BEFORE
                                              # looking up "today's material" below, so a same-day rollover
                                              # (a debt from a day that just passed) is already reflected
            self._refresh_todays_material_and_debt()

    def _refresh_todays_material_and_debt(self):
        """Looks up today's scheduled material once and, if there's
        outstanding debt for that course/field (see session_snapshot.py
        -- unfinished study time from an earlier early-leave), bumps
        total_minutes_spin up by the owed amount right away. The debt
        itself isn't cleared here -- only once _begin_lockdown_flow()
        actually commits to running that session, in case the user never
        starts it and the debt would otherwise be lost unpaid."""
        try:
            from study_planner_panel import find_todays_material
            pdf_path, topics, session = find_todays_material(self._study_planner)
        except Exception:
            pdf_path, topics, session = None, [], None
        self._todays_material = (pdf_path, topics, session)

        if session is None:
            return
        import session_snapshot
        debt = session_snapshot.load_debt(session["course"], session["field"])
        if debt is None:
            return
        owed_minutes = max(1, round(debt["shortfall_seconds"] / 60))
        self.total_minutes_spin.setValue(self.total_minutes_spin.value() + owed_minutes)
        self.monitor_status_label.setText(
            f"+{owed_minutes} min added to today's session -- carried over from an unfinished "
            f"{session['course']} {session['field']} session ({debt['completion_pct']}% done last time)."
        )

    def _refresh_penalty_banner(self):
        """Shows/hides the soft push-ups-owed nudge based on whatever
        session_snapshot currently has on record -- called once at
        startup, and again right after PenaltyCompletionDialog closes
        (whether it actually finished the reps or was just dismissed)."""
        import session_snapshot
        penalty = session_snapshot.load_penalty()
        if penalty is None:
            self.penalty_banner.setVisible(False)
            self.penalty_complete_button.setVisible(False)
            return
        self.penalty_banner.setText(
            f"You have {penalty['reps_owed']} push-up(s) owed from an interrupted penalty -- "
            "this doesn't block studying, but it's still owed."
        )
        self.penalty_banner.setVisible(True)
        self.penalty_complete_button.setVisible(True)

    def _on_penalty_complete_clicked(self):
        import session_snapshot
        penalty = session_snapshot.load_penalty()
        if penalty is None:
            self._refresh_penalty_banner()
            return
        dialog = PenaltyCompletionDialog(penalty["reps_owed"], parent=self)
        dialog.exec()
        self._refresh_penalty_banner()  # reflects whatever happened -- cleared if actually completed,
                                         # unchanged if just dismissed

    def _check_calendar_rollover(self):
        """For any outstanding debt (session_snapshot.py) from a day
        that's already PASSED (not today -- today's own session might
        still happen), moves that day's now-stale calendar entry forward
        to today via StudyPlanner.roll_over_incomplete_day(), so there's
        actually a scheduled slot for the make-up session to land on.
        Only touches the CALENDAR ENTRY's date -- the debt-minutes ledger
        itself is handled separately (folded into total_minutes_spin,
        see _refresh_todays_material_and_debt()). Idempotent: once an
        entry's been moved, a repeat check simply won't find anything
        left on the old date, so calling this on every launch is safe.
        Best-effort -- a Notion failure here shouldn't block the rest of
        the app from loading."""
        if self._study_planner is None:
            return
        import session_snapshot
        from datetime import date as date_cls
        today = date_cls.today()
        try:
            for entry in session_snapshot.load_all_debts().values():
                saved_date = date_cls.fromisoformat(entry["saved_at"][:10])
                if saved_date < today:
                    self._study_planner.roll_over_incomplete_day(entry["course"], entry["field"], saved_date, today)
        except Exception as e:
            print(f"[FocusSessionWidget] calendar day-rollover check failed (non-fatal): {e}")

    def _is_monitor_suppressed(self):
        """True while on a break OR while lockdown is exited mid-session --
        the monitor thread keeps running full-tilt in both cases (see this
        class's docstring for why), it just stands down and doesn't act on
        or accumulate anything it sees."""
        return self._break_event.is_set() or self._lockdown_paused_event.is_set()

    def _on_total_minutes_changed(self, value):
        self.timer_widget.set_total_minutes(value)

    def start_custom_session(self, pdf_path, total_minutes, total_slides=0):
        """Starts a session with a SPECIFIC material file and total
        duration instead of auto-picking from today's schedule -- used by
        the Revision panel (upload slides directly, get an estimated/
        adjusted total time, launch). total_slides (optional): the exact
        slide count the caller's time estimate was based on, so
        LockdownWindow's pacing progress bar can be exact rather than
        falling back to the PDF's raw page count. Only valid before any
        session has started; a no-op otherwise, same as every other
        pre-session setter here (set_total_minutes(), etc.)."""
        if self.timer_widget.phase != "not_started":
            return
        self._forced_pdf_path = pdf_path
        self._forced_total_slides = total_slides
        self.total_minutes_spin.setValue(total_minutes)  # keeps the visible field in sync with what's actually used
        self.timer_widget.set_total_minutes(total_minutes)
        self.timer_widget.start_clicked()

    def _on_session_starting(self):
        self.total_minutes_spin.setEnabled(False)  # the block sequence is locked in once a session actually starts
        self.monitor_status_label.setText("Distraction monitor: loading webcam and models...")
        self._thread = VisionMonitorThread(
            is_suppressed=self._is_monitor_suppressed,
            on_reset_requested=self._reset_flag.consume,
        )
        self._thread.ready.connect(self._on_monitor_ready)
        self._thread.disruption_changed.connect(self._on_disruption_changed)
        self._thread.penalty_pending_changed.connect(self._on_penalty_pending_changed)
        self._thread.failed.connect(self._on_monitor_failed)
        self._thread.stopped.connect(self._on_monitor_stopped)
        self._thread.frame_ready.connect(self._on_frame_ready)
        self._thread.start()

    def _on_monitor_ready(self):
        self.monitor_status_label.setText("Distraction monitor: watching...")
        self._begin_lockdown_flow()

    def _begin_lockdown_flow(self):
        if self._forced_pdf_path is not None:
            # A Revision session (or any other caller of
            # start_custom_session()) already picked its own material --
            # skip the auto-pick lookup AND the topic-preview dialog
            # entirely (that dialog previews a Study Planner plan's
            # precomputed topics, which don't exist for this path).
            pdf_path = self._forced_pdf_path
            self._forced_pdf_path = None  # consumed -- a later Resume Lockdown reopens via _last_pdf_path, not this
            self._current_total_slides = self._forced_total_slides
            self._forced_total_slides = 0
            self._current_session_scheduled = None  # a Revision/forced session isn't on the calendar
        else:
            # Reuses the SAME lookup _refresh_todays_material_and_debt()
            # already did at construction time (that's what any debt-
            # adjusted total_minutes_spin bump was computed against) --
            # not looked up fresh here, so the material actually used
            # matches whatever the debt-folding logic saw.
            pdf_path, topics, session = self._todays_material
            self._current_total_slides = 0  # no explicit slide count for an auto-picked session -- LockdownWindow
                                             # falls back to the PDF's real page count
            self._current_session_scheduled = session
            self._current_deadline_passed = False
            if session is not None:
                # Committing to actually run this session now -- any debt
                # for this course/field was already folded into total_
                # minutes_spin's value up front, so it's paid off here,
                # not left to linger (see _refresh_todays_material_and_debt()).
                import session_snapshot
                session_snapshot.clear_debt(session["course"], session["field"])

                # Strong-but-still-escapable warning if this course/
                # field's deadline has already passed with the material
                # not yet done -- shown INSIDE lockdown (see _open_
                # lockdown()), never a true block: Esc/Ctrl+Shift+Alt/
                # Exit Lockdown always work in this app, that's not
                # changing here.
                if self._study_planner is not None:
                    try:
                        from datetime import date as date_cls
                        exams = self._study_planner.list_exams()
                        match = next(
                            (e for e in exams if e["course"] == session["course"] and e["field"] == session["field"]),
                            None,
                        )
                        if match is not None:
                            self._current_deadline_passed = date_cls.today() > match["date"]
                    except Exception:
                        self._current_deadline_passed = False

            # Mandatory "Preview State": if there's material to summarize,
            # show the topic table before lockdown activates (see
            # topic_preview.py for what "mandatory" actually means -- it
            # can't be skipped instantly, but a failed/missing extraction
            # still doesn't trap you). No material to preview -> nothing to
            # show, straight through.
            if pdf_path and self._study_planner is not None:
                from topic_preview import TopicPreviewDialog
                preview = TopicPreviewDialog(
                    topics=topics, llm_client=self._study_planner.llm, pdf_path=pdf_path, parent=self
                )
                preview.exec()

        # The countdown starts here, AFTER the preview resolves -- not
        # before it (as it briefly was), which could run the whole timer
        # out while you were still stuck waiting on a slow LLM call that
        # was blocked behind a modal dialog you couldn't even see progress
        # on. With topics precomputed (see StudyPlanner.generate_plan()),
        # this dialog is normally instant anyway.
        self._last_pdf_path = pdf_path
        self.timer_widget.begin_study_countdown()
        self._open_lockdown(pdf_path)

    def _open_lockdown(self, pdf_path):
        self.resume_lockdown_button.setVisible(False)
        self._layout.removeWidget(self.timer_widget)
        from lockdown_window import LockdownWindow
        self._lockdown = LockdownWindow(
            pdf_path, self.timer_widget, total_slides=self._current_total_slides,
            deadline_passed=self._current_deadline_passed,
        )
        self._lockdown.exit_requested.connect(self._close_lockdown)
        self._lockdown.material_completed.connect(self._on_material_completed)
        self._lockdown.start_lockdown()

    def _on_material_completed(self):
        """Fires once LockdownWindow's own 100%-completion review flow
        resolves (see LockdownWindow._on_material_completed()) -- marks
        the matching Notion task done, if this was a calendar-linked
        session. Async (a real Notion API call), best-effort: a failure
        here is logged, not surfaced -- the study session itself has
        already moved on by this point (finish_block_early() already
        ran), there's nothing left for a failure to meaningfully block."""
        if self._current_session_scheduled is None or self._study_planner is None:
            return
        from datetime import date as date_cls
        course = self._current_session_scheduled["course"]
        field = self._current_session_scheduled["field"]
        self._complete_worker = MarkCompletedWorker(self._study_planner, course, field, date_cls.today())
        self._complete_worker.done.connect(self._on_mark_completed_done)
        self._complete_worker.failed.connect(self._on_mark_completed_failed)
        self._complete_worker.start()

    def _on_mark_completed_done(self, found):
        if found:
            print("[FocusSessionWidget] marked today's Notion task complete")
        else:
            print("[FocusSessionWidget] no matching Notion calendar entry found to mark complete (already handled?)")

    def _on_mark_completed_failed(self, message):
        print(f"[FocusSessionWidget] couldn't mark the Notion task complete: {message}")

    def _on_frame_ready(self, main_frame, pose_frame):
        if self._lockdown is not None:
            self._lockdown.update_frame(main_frame, pose_frame)

    def _close_lockdown(self, offer_resume=True):
        # Exiting lockdown returns to the normal view. When it's a genuine
        # mid-session exit (Esc/button -- offer_resume=True, the default a
        # no-arg signal connection lands on), the countdown pauses (see
        # pause_for_lockdown_exit()) rather than silently running
        # unmonitored, and Resume Lockdown reopens it. When it's really the
        # session/monitor ending (offer_resume=False, from
        # _on_session_finished/_on_monitor_failed/_on_monitor_stopped)
        # there's nothing to resume, so the button stays hidden.
        #
        # The timer widget is detached and re-parented immediately (no fade
        # on the thing you actually care about watching); the rest of the
        # lockdown shell fades out behind it via animate_close().
        if self._lockdown is None:
            return
        lockdown = self._lockdown
        self._lockdown = None

        self._persist_penalty_if_owed()

        # Snapshot exactly where this left off -- but only for a
        # CALENDAR-linked session (a Revision/forced session has nothing
        # to carry over) that's genuinely incomplete. phase == "done" is
        # what _on_session_finished's path looks like by the time it gets
        # here (StudyTimerWidget sets it right before emitting finished),
        # so checking it here -- rather than threading a separate flag
        # through every _close_lockdown() caller (mid-exit, monitor
        # failed, monitor stopped, genuine completion all reach this same
        # method) -- correctly clears on real completion and saves on
        # everything else, using state that already exists.
        if self._current_session_scheduled is not None:
            import session_snapshot
            course = self._current_session_scheduled["course"]
            field = self._current_session_scheduled["field"]
            if self.timer_widget.phase == "done":
                session_snapshot.clear_debt(course, field)
            elif lockdown._effective_total_slides > 0:
                slide_number = (lockdown._current_visible_page() or 0) + 1
                session_snapshot.save_debt(
                    course, field, self._last_pdf_path, slide_number, lockdown._effective_total_slides, self.timer_widget,
                )

        # Persist this session's distraction/engagement-judgment totals
        # (see LockdownWindow.get_engagement_summary()) to local history
        # -- previously this data only ever lived in memory for the
        # session's duration, driving the pacing bar, then discarded.
        # Logged for EVERY session, not just calendar-linked ones (a
        # Revision/forced session's distraction data is just as real,
        # course/field just come back "" -> the dashboard's
        # "(unspecified)" bucket). log_session() itself skips a session
        # with no real study time elapsed, so a lockdown opened and
        # immediately closed doesn't clutter the log.
        import engagement_log
        course = self._current_session_scheduled["course"] if self._current_session_scheduled else ""
        field = self._current_session_scheduled["field"] if self._current_session_scheduled else ""
        engagement_log.log_session(course, field, lockdown.get_engagement_summary())

        timer_widget = lockdown.take_timer_widget()
        if timer_widget is not None:
            self._layout.insertWidget(0, timer_widget)
        lockdown.animate_close()

        if offer_resume and self.timer_widget.pause_for_lockdown_exit():
            self._lockdown_paused_event.set()
            self.resume_lockdown_button.setVisible(True)
            self.monitor_status_label.setText(
                "Distraction monitor: watching, but not paying attention to distractions -- "
                "Resume Lockdown to continue your timer."
            )
        else:
            self.resume_lockdown_button.setVisible(False)

    def _on_resume_lockdown_clicked(self):
        self.resume_lockdown_button.setVisible(False)
        self._lockdown_paused_event.clear()
        self.monitor_status_label.setText("Distraction monitor: watching...")
        self.timer_widget.resume_after_lockdown_exit()
        self._open_lockdown(self._last_pdf_path)

    def _on_disruption_changed(self, is_disruptive):
        if is_disruptive:
            self.timer_widget.pause_for_distraction()
            self.monitor_status_label.setText(
                "Distraction monitor: paused -- clear the pose challenge / push-ups!"
            )
        else:
            self.timer_widget.resume_after_distraction()
            self.monitor_status_label.setText("Distraction monitor: watching...")

    def _on_penalty_pending_changed(self, pending, reps_done, reps_needed):
        """Just tracks the LIVE state -- fires continuously while owed
        (see gaze_engine.main()) so whatever's cached here is accurate at
        whatever moment an exit actually happens; the persist decision
        itself lives at each exit point (_close_lockdown(), the app
        closing), not here."""
        self._penalty_pending = pending
        self._penalty_reps_done = reps_done
        self._penalty_reps_needed = reps_needed

    def _persist_penalty_if_owed(self):
        """Called from every real exit point (mid-session lockdown close,
        the app closing) -- if push-ups were actively owed at that exact
        moment, saves however many reps were still left so next launch
        can nudge paying them off (see session_snapshot.save_penalty() /
        PenaltyCompletionDialog)."""
        if self._penalty_pending:
            import session_snapshot
            session_snapshot.save_penalty(max(0, self._penalty_reps_needed - self._penalty_reps_done))

    def _on_session_finished(self):
        self._close_lockdown(offer_resume=False)
        if self._thread is not None:
            self._thread.request_stop()

    def _on_monitor_failed(self, message):
        self._close_lockdown(offer_resume=False)
        QMessageBox.warning(self, "Distraction monitor error", message)
        self.timer_widget.cancel_loading()

    def _on_monitor_stopped(self):
        self._close_lockdown(offer_resume=False)
        self.monitor_status_label.setText("Distraction monitor: not running.")
        self._thread = None


class AddTodoWorker(QThread):
    """Adds one to-do item to the to-do list page."""
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, todo_page, text):
        super().__init__()
        self.todo_page = todo_page
        self.text = text

    def run(self):
        try:
            self.todo_page.add_todo(self.text)
            self.done.emit(self.text)
        except Exception as e:
            self.failed.emit(str(e))


class FetchTodosWorker(QThread):
    """Lists the to-do page's current items -- run whenever the list
    needs a fresh read (tab shown, after add/toggle/delete)."""
    done = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, todo_page):
        super().__init__()
        self.todo_page = todo_page

    def run(self):
        try:
            self.done.emit(self.todo_page.list_todos())
        except Exception as e:
            self.failed.emit(str(e))


class ToggleTodoWorker(QThread):
    """Checks/unchecks one to-do item."""
    done = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, todo_page, block_id, checked):
        super().__init__()
        self.todo_page = todo_page
        self.block_id = block_id
        self.checked = checked

    def run(self):
        try:
            self.todo_page.set_todo_checked(self.block_id, self.checked)
            self.done.emit()
        except Exception as e:
            self.failed.emit(str(e))


class DeleteTodoWorker(QThread):
    """Archives (deletes) one to-do item."""
    done = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, todo_page, block_id):
        super().__init__()
        self.todo_page = todo_page
        self.block_id = block_id

    def run(self):
        try:
            self.todo_page.delete_todo(self.block_id)
            self.done.emit()
        except Exception as e:
            self.failed.emit(str(e))


class WeekSettingsWidget(QWidget):
    """The To-Do List tab: add/view/check-off/delete items on the Notion
    to-do list, plus Passive Tracking -- a plain start/stop stopwatch for
    non-exam work (to-dos, assignments, projects) with NO webcam, NO
    distraction enforcement, NO penalties, deliberately separate from
    Monitor Lockdown's whole pipeline (see core/passive_tracking.py).
    The "week 1 starts on" date that drives the to-do page's auto-
    syncing title lives in Settings, not here."""

    def __init__(self, todo_page, parent=None):
        super().__init__(parent)
        self.todo_page = todo_page
        self._todo_worker = None
        self._fetch_worker = None
        self._toggle_worker = None
        self._delete_worker = None
        self._tracking_started_at = None  # time.time() while a passive-tracking session is running, else None

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("<b>Add a to-do item</b>"))
        todo_row = QHBoxLayout()
        self.todo_input = QLineEdit()
        self.todo_input.setPlaceholderText("e.g. buy a new mouse")
        self.todo_input.returnPressed.connect(self._on_add_todo_clicked)
        todo_row.addWidget(self.todo_input)
        self.add_todo_button = QPushButton("Add")
        self.add_todo_button.clicked.connect(self._on_add_todo_clicked)
        todo_row.addWidget(self.add_todo_button)
        layout.addLayout(todo_row)

        self.todo_status_label = QLabel("")
        layout.addWidget(self.todo_status_label)

        # ---- to-do list view: previously add-only, with no way to even
        # see what's already on the list from inside the app ----
        list_header = QHBoxLayout()
        list_header.addWidget(QLabel("<b>Your to-dos</b>"))
        list_header.addStretch()
        self.refresh_todos_button = QPushButton("Refresh")
        self.refresh_todos_button.clicked.connect(self.refresh_todos)
        list_header.addWidget(self.refresh_todos_button)
        layout.addLayout(list_header)

        self.todo_list_layout = QVBoxLayout()
        layout.addLayout(self.todo_list_layout)

        # ---- Passive Tracking: a plain stopwatch, no lockdown ----
        tracking_card = Card()
        tracking_layout = QVBoxLayout(tracking_card)
        tracking_layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(tracking_card)

        tracking_layout.addWidget(QLabel("<b>Passive Tracking</b>"))
        tracking_note = QLabel(
            "A plain timer for non-exam work -- no webcam, no distraction monitoring, no penalties. "
            "Click a to-do above to fill in what you're tracking, or just type your own."
        )
        tracking_note.setWordWrap(True)
        tracking_layout.addWidget(tracking_note)

        tracking_row = QHBoxLayout()
        self.tracking_description_edit = QLineEdit()
        self.tracking_description_edit.setPlaceholderText("What are you working on?")
        tracking_row.addWidget(self.tracking_description_edit)
        self.tracking_button = QPushButton("Start Tracking")
        self.tracking_button.clicked.connect(self._on_tracking_button_clicked)
        tracking_row.addWidget(self.tracking_button)
        tracking_layout.addLayout(tracking_row)

        self.tracking_elapsed_label = QLabel("")
        tracking_layout.addWidget(self.tracking_elapsed_label)

        self._tracking_timer = QTimer(self)
        self._tracking_timer.timeout.connect(self._update_tracking_elapsed)

        self.tracking_today_label = QLabel("")
        tracking_layout.addWidget(self.tracking_today_label)
        self._refresh_todays_tracking_total()

        layout.addStretch()

        if self.todo_page is None:
            self.todo_input.setEnabled(False)
            self.add_todo_button.setEnabled(False)
            self.refresh_todos_button.setEnabled(False)
            self.todo_status_label.setText("No to-do list page configured.")
        else:
            self.refresh_todos()

    # ---- adding ----

    def _on_add_todo_clicked(self):
        text = self.todo_input.text().strip()
        if not text or self.todo_page is None:
            return
        self.todo_input.setEnabled(False)
        self.add_todo_button.setEnabled(False)
        self.todo_status_label.setText("Adding...")
        self._todo_worker = AddTodoWorker(self.todo_page, text)
        self._todo_worker.done.connect(self._on_add_todo_done)
        self._todo_worker.failed.connect(self._on_add_todo_failed)
        self._todo_worker.start()

    def _on_add_todo_done(self, text):
        self.todo_input.setEnabled(True)
        self.add_todo_button.setEnabled(True)
        self.todo_input.clear()
        self.todo_status_label.setText(f"Added: \"{text}\"")
        self.refresh_todos()

    def _on_add_todo_failed(self, message):
        self.todo_input.setEnabled(True)
        self.add_todo_button.setEnabled(True)
        self.todo_status_label.setText(f"Couldn't add: {message}")

    # ---- listing / checking off / deleting ----

    def refresh_todos(self):
        if self.todo_page is None or (self._fetch_worker is not None and self._fetch_worker.isRunning()):
            return
        self.refresh_todos_button.setEnabled(False)
        self._fetch_worker = FetchTodosWorker(self.todo_page)
        self._fetch_worker.done.connect(self._on_todos_fetched)
        self._fetch_worker.failed.connect(self._on_todos_fetch_failed)
        self._fetch_worker.start()

    def _clear_todo_list_layout(self):
        while self.todo_list_layout.count():
            item = self.todo_list_layout.takeAt(0)
            child_layout = item.layout()
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif child_layout is not None:
                while child_layout.count():
                    sub_item = child_layout.takeAt(0)
                    if sub_item.widget() is not None:
                        sub_item.widget().deleteLater()

    def _on_todos_fetched(self, items):
        self.refresh_todos_button.setEnabled(True)
        self._clear_todo_list_layout()
        if not items:
            self.todo_list_layout.addWidget(QLabel("Nothing on the list right now."))
            return
        for item in items:
            # A real widget, not a bare layout -- a QHBoxLayout has no
            # paintable surface of its own, so it couldn't take the new
            # rounded-card row styling at all (added directly via
            # addLayout() before). #TodoRow is an ID-scoped rule (see
            # theme.py's build_stylesheet()) so it styles only this row's
            # own background, not the checkbox/buttons inside it.
            row_widget = QWidget()
            row_widget.setObjectName("TodoRow")
            row_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(12, 8, 12, 8)

            checkbox = QCheckBox(item["text"])
            checkbox.setChecked(item["checked"])
            checkbox.toggled.connect(
                lambda checked, block_id=item["id"]: self._on_todo_toggled(block_id, checked)
            )
            row.addWidget(checkbox, 1)

            track_button = QPushButton("Track")
            track_button.setToolTip("Fill the Passive Tracking description with this item's text")
            track_button.clicked.connect(
                lambda _checked, text=item["text"]: self.tracking_description_edit.setText(text)
            )
            row.addWidget(track_button)

            delete_button = QPushButton("Delete")
            delete_button.clicked.connect(lambda _checked, block_id=item["id"]: self._on_todo_delete_clicked(block_id))
            row.addWidget(delete_button)

            self.todo_list_layout.addWidget(row_widget)

    def _on_todos_fetch_failed(self, message):
        self.refresh_todos_button.setEnabled(True)
        self._clear_todo_list_layout()
        self.todo_list_layout.addWidget(QLabel(f"Couldn't load to-dos: {message}"))

    def _on_todo_toggled(self, block_id, checked):
        self._toggle_worker = ToggleTodoWorker(self.todo_page, block_id, checked)
        self._toggle_worker.failed.connect(
            lambda message: self.todo_status_label.setText(f"Couldn't update that item: {message}")
        )
        self._toggle_worker.start()

    def _on_todo_delete_clicked(self, block_id):
        self._delete_worker = DeleteTodoWorker(self.todo_page, block_id)
        self._delete_worker.done.connect(self.refresh_todos)
        self._delete_worker.failed.connect(
            lambda message: self.todo_status_label.setText(f"Couldn't delete that item: {message}")
        )
        self._delete_worker.start()

    # ---- passive tracking (local only -- see core/passive_tracking.py) ----

    def _on_tracking_button_clicked(self):
        if self._tracking_started_at is None:
            self._tracking_started_at = time.time()
            self.tracking_button.setText("Stop Tracking")
            self.tracking_description_edit.setEnabled(False)
            self._tracking_timer.start(1000)
            self._update_tracking_elapsed()
        else:
            elapsed_minutes = (time.time() - self._tracking_started_at) / 60.0
            self._tracking_timer.stop()
            self._tracking_started_at = None
            self.tracking_button.setText("Start Tracking")
            self.tracking_description_edit.setEnabled(True)
            passive_tracking.log_session(self.tracking_description_edit.text().strip(), elapsed_minutes)
            self.tracking_elapsed_label.setText(f"Logged {elapsed_minutes:.1f} min.")
            self.tracking_description_edit.clear()
            self._refresh_todays_tracking_total()

    def _update_tracking_elapsed(self):
        if self._tracking_started_at is None:
            return
        elapsed = int(time.time() - self._tracking_started_at)
        minutes, seconds = divmod(elapsed, 60)
        self.tracking_elapsed_label.setText(f"Elapsed: {minutes:02d}:{seconds:02d}")

    def _refresh_todays_tracking_total(self):
        total = passive_tracking.todays_total_minutes()
        self.tracking_today_label.setText(f"Today's passive tracking: {total:.1f} min")


def _missing_config_notice(text):
    widget = QWidget()
    layout = QVBoxLayout(widget)
    label = QLabel(text)
    label.setWordWrap(True)
    layout.addWidget(label)
    layout.addStretch()
    return widget


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Study Maxxing")
        self.resize(1400, 800)

        self._app_settings = QSettings(SETTINGS_ORG, SETTINGS_APP)

        central = QWidget()
        self.setCentralWidget(central)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)

        header = HeaderBanner()
        header.settings_requested.connect(self._open_settings)
        header.games_requested.connect(lambda: self._open_game_picker(header.games_button))
        central_layout.addWidget(header, 0)

        splitter = _themed_splitter(Qt.Orientation.Horizontal)
        central_layout.addWidget(splitter, 1)

        # Left side: the app's pages as toggleable (collapsible) sections
        # rather than tabs, so any combination can be open at once -- AND
        # stacked in a vertical splitter rather than a plain layout, so you
        # can drag the handle between any two pages to stretch one at the
        # other's expense instead of being stuck with whatever height each
        # one's content naturally wants.
        pages_splitter = _themed_splitter(Qt.Orientation.Vertical)
        pages_splitter.setChildrenCollapsible(False)  # dragging resizes pages, it doesn't hide them -- that's what each page's own header toggle is for

        notion_token = _resolve_setting("NOTION_TOKEN", self._app_settings, KEY_NOTION_TOKEN, DEFAULT_NOTION_TOKEN)
        table_block_id = _resolve_setting(
            "NOTION_TABLE_BLOCK_ID", self._app_settings, KEY_NOTION_TABLE_BLOCK_ID, DEFAULT_NOTION_TABLE_BLOCK_ID
        )
        todo_page_id = _resolve_setting(
            "NOTION_TODO_PAGE_ID", self._app_settings, KEY_NOTION_TODO_PAGE_ID, DEFAULT_NOTION_TODO_PAGE_ID
        )
        calendar_database_id = _resolve_setting(
            "NOTION_CALENDAR_DATABASE_ID", self._app_settings, KEY_NOTION_CALENDAR_DATABASE_ID,
            DEFAULT_NOTION_CALENDAR_DATABASE_ID,
        )
        self._current_notion = {
            "token": notion_token,
            "table_block_id": table_block_id,
            "todo_page_id": todo_page_id,
            "calendar_database_id": calendar_database_id,
        }

        todo_page = NotionPageClient(notion_token, todo_page_id) if notion_token and todo_page_id else None
        self._todo_page = todo_page

        # Monitor Lockdown (see FocusSessionWidget) needs these to look up
        # today's material -- still works fine without them, just won't
        # find anything to show full-screen.
        focus_widget = FocusSessionWidget(
            notion_token=notion_token, table_block_id=table_block_id, calendar_database_id=calendar_database_id
        )
        self._focus_widget = focus_widget  # closeEvent() reaches back in to persist any owed penalty debt
        pages_splitter.addWidget(
            CollapsibleSection("Focus Session", focus_widget, help_text=FOCUS_SESSION_HELP_TEXT)
        )

        # No Notion involvement at all -- just uploads a PDF directly and
        # launches a session through the SAME FocusSessionWidget above,
        # via start_custom_session().
        revision_widget = RevisionPanel()
        revision_widget.session_requested.connect(focus_widget.start_custom_session)
        pages_splitter.addWidget(
            CollapsibleSection("Revision", revision_widget, help_text=REVISION_HELP_TEXT)
        )

        pages_splitter.addWidget(
            CollapsibleSection("To-Do List", WeekSettingsWidget(todo_page), help_text=TODO_LIST_HELP_TEXT)
        )

        pages_splitter.addWidget(
            CollapsibleSection("Progress", EngagementDashboardWidget(), help_text=ENGAGEMENT_DASHBOARD_HELP_TEXT)
        )

        if notion_token and table_block_id:
            schedule_widget = ExamFormPanel(
                notion_token=notion_token, table_block_id=table_block_id,
                calendar_database_id=calendar_database_id, todo_page=todo_page,
            )
        else:
            schedule_widget = _missing_config_notice(
                "Schedule Assistant needs the NOTION_TOKEN and NOTION_TABLE_BLOCK_ID "
                "environment variables set before it can talk to your Notion table."
            )
        pages_splitter.addWidget(
            CollapsibleSection("Schedule Assistant", schedule_widget, help_text=EXAM_FORM_HELP_TEXT)
        )

        if notion_token and table_block_id and calendar_database_id:
            planner_widget = StudyPlannerPanel(
                notion_token=notion_token, table_block_id=table_block_id,
                calendar_database_id=calendar_database_id,
            )
        else:
            planner_widget = _missing_config_notice(
                "Study Planner needs the NOTION_TOKEN, NOTION_TABLE_BLOCK_ID, and "
                "NOTION_CALENDAR_DATABASE_ID environment variables set."
            )
        pages_splitter.addWidget(
            CollapsibleSection("Study Planner", planner_widget, help_text=STUDY_PLANNER_HELP_TEXT)
        )

        pages_scroll = QScrollArea()
        pages_scroll.setWidgetResizable(True)
        # Never show a horizontal scrollbar -- the pages panel should just
        # stretch to whatever width the splitter gives it, not force you to
        # scroll sideways to see cut-off content.
        pages_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        pages_scroll.setWidget(pages_splitter)
        splitter.addWidget(pages_scroll)

        notion_embed_url = _resolve_setting(
            "NOTION_EMBED_URL", self._app_settings, KEY_NOTION_EMBED_URL, DEFAULT_NOTION_EMBED_URL
        )
        self._current_notion["embed_url"] = notion_embed_url
        notion_view = QWebEngineView()
        # Both kept on self so they aren't garbage-collected out from under the view.
        self._notion_profile = _create_notion_profile(notion_view)
        self._notion_page = NotionWebPage(self._notion_profile, notion_view)
        notion_view.setPage(self._notion_page)
        notion_view.setUrl(QUrl(notion_embed_url))
        splitter.addWidget(notion_view)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([460, 940])  # pages panel starts narrower -- it doesn't need as much room as Notion

    def _open_settings(self):
        current_lm_studio_url = self._app_settings.value(KEY_LM_STUDIO_URL) or None
        dialog = SettingsDialog(
            todo_page=self._todo_page,
            current_notion=self._current_notion,
            current_lm_studio_url=current_lm_studio_url,
            parent=self,
        )
        dialog.exec()

    def _open_game_picker(self, button):
        # Lazy import, matching _open_lockdown()'s own pattern in this
        # file -- overlay.py pulls in every game module, no need to pay
        # that cost at every app startup for a menu that might never
        # get opened.
        from overlay import build_game_picker_menu
        menu = build_game_picker_menu(self)
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def closeEvent(self, event):
        # If the app closes while push-ups are actively owed (not just a
        # mid-session lockdown exit, which _close_lockdown() already
        # covers), that debt still needs to persist for next launch.
        self._focus_widget._persist_penalty_if_owed()
        super().closeEvent(event)
