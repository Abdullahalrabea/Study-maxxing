# "Monitor Lockdown": a full-screen view shown while the distraction
# monitor is running. Today's study material (a PDF, auto-picked via
# study_planner_panel.find_todays_pdf) fills most of the screen, the live
# webcam feed (fed frame-by-frame from VisionMonitorThread.frame_ready) sits
# top-left, and the actual focus timer widget is re-parented in up top-center
# for the duration -- see take_timer_widget() for handing it back once
# lockdown ends.
#
# start_lockdown() is what actually goes "live": positions this window
# full-screen on monitor 1, launches/tiles Discord+Spotify on monitor 2,
# and starts minimizing every other window (re-enforced every couple
# seconds -- see lockdown_os.py for why this is deliberately a "soft"
# measure, not a keyboard hook). Plain construction has none of these
# side effects, only start_lockdown() does -- so building/inspecting this
# widget (e.g. in tests) never touches the real desktop.
#
# Not meant to be inescapable: an "Exit Lockdown" button, Escape, and
# Ctrl+Shift+Alt (works regardless of which window has focus) all always
# get you back to the normal window. The point is removing the temptation
# to tab away, not trapping you if something's wrong.

import time
from datetime import datetime

import numpy as np
import fitz  # PyMuPDF
from PyQt6.QtCore import Qt, QEvent, QTimer, QThread, QPropertyAnimation, QEasingCurve, pyqtSignal, QSettings
from PyQt6.QtGui import QImage, QPixmap, QPainter
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QGraphicsOpacityEffect, QDialog, QFrame
)

import lockdown_os
import theme
from annotation_overlay import DrawingCanvas, AnnotationToolbar, ScratchpadPanel
from attention_video import AttentionVideoPlayer
from voice_assistant_panel import VoiceAssistantPanel
from spotify_widget import SpotifyNowPlayingWidget
from llm_client import LLMClient
from paths import get_resource_dir

WEBCAM_LABEL_SIZE = (320, 240)
POSE_LABEL_SIZE = (280, 220)  # sized to sit prominently next to the webcam, matching the user's own wireframe
DIMMED_OPACITY = 0.25  # how much the PDF fades while a pose challenge is up
COMPANION_APPS_MONITOR = 1  # "monitor 2" (0-based) -- where Discord/Spotify get tiled
FADE_MS = 300  # entrance/exit fade duration -- the one deliberate motion moment for the highest-stakes transition in the app
LOCKDOWN_MONITOR = 0         # "monitor 1" -- where this window itself goes full-screen
MAX_RENDERED_PAGES = 80      # sane cap so a huge PDF can't stall the UI or eat all your RAM
ZOOM_STEP = 1.2
MIN_ZOOM = 0.4
MAX_ZOOM = 4.0
PACE_UPDATE_INTERVAL_MS = 2000  # how often the pacing bar re-checks elapsed time + actual scroll position
PACE_TOLERANCE_SLIDES = 1  # +-1 slide never counts as "behind"/"ahead" -- real reading pace naturally
                            # wobbles a little around a rigid time/slide-count formula; only flag a
                            # genuine gap, not every sub-slide rounding difference

ENGAGEMENT_WINDOW_MS = 90_000  # how often a short window's distraction data gets sent to the LLM for a
                                # credit judgment -- see _run_engagement_judgment()
CREDIT_MULTIPLIERS = {"full": 1.0, "reduced": 0.5, "none": 0.0}  # how much of a judged window's elapsed
                                                                  # time actually counts toward the pacing
                                                                  # bar's progress

HEART_IMAGE_PATH = get_resource_dir("ui/Images") / "minecraft-heart.png"
HEART_SIZE = 32           # each heart's rendered width/height, px
HEART_SPACING = 4         # gap between hearts, px
HEART_COUNT = 10          # 10 hearts * 2 half-steps each = 20 steps, 5% granularity -- same convention as a
                           # classic game health bar
HEART_DIMMED_OPACITY = 0.25

SIDEBAR_WIDTH = 520  # right-hand Notepad/YouTube column -- wide enough for AttentionVideoPlayer's fixed
                      # 480px-wide player plus real margins on both sides


def _vline():
    # A flat, explicitly-colored 1px line via QSS (theme.py's
    # "#themed-divider" rule), not QFrame's native VLine/Sunken 3D
    # groove -- that groove's highlight/shadow contrast is built for a
    # light OS theme and washes out to near-invisible against this
    # app's dark palette (confirmed via a real render: the header's
    # zone dividers didn't show up at all).
    line = QFrame()
    line.setObjectName("themed-divider")
    line.setFrameShape(QFrame.Shape.NoFrame)
    line.setFixedWidth(1)
    return line


def _hline():
    line = QFrame()
    line.setObjectName("themed-divider")
    line.setFrameShape(QFrame.Shape.NoFrame)
    line.setFixedHeight(1)
    return line


def _bgr_to_pixmap(frame_bgr):
    frame_rgb = np.ascontiguousarray(frame_bgr[:, :, ::-1])  # BGR -> RGB, contiguous for QImage
    height, width, channels = frame_rgb.shape
    qimage = QImage(frame_rgb.data, width, height, channels * width, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimage)


class HeartBar(QWidget):
    """A row of heart icons -- Zelda/Minecraft-style visual for the
    lockdown pacing progress (see LockdownWindow._update_pace()), instead
    of a plain progress bar. set_fraction(0.0-1.0) fills hearts left to
    right in HALF-HEART steps (HEART_COUNT * 2 total steps: each heart is
    dimmed -> half-undimmed -> fully-undimmed before the next one starts
    filling) -- a stepped game-style fill, not a smooth continuous one."""

    def __init__(self, heart_count=HEART_COUNT, parent=None):
        super().__init__(parent)
        self.heart_count = heart_count
        self._fraction = 0.0
        self._heart_pixmap = QPixmap(str(HEART_IMAGE_PATH)).scaled(
            HEART_SIZE, HEART_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation
        )
        total_width = heart_count * HEART_SIZE + max(0, heart_count - 1) * HEART_SPACING
        self.setFixedSize(total_width, HEART_SIZE)

    def set_fraction(self, fraction):
        fraction = max(0.0, min(1.0, fraction))
        if fraction != self._fraction:
            self._fraction = fraction
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)  # keep the pixel art crisp, not blurred
        total_steps = self.heart_count * 2
        current_step = round(self._fraction * total_steps)
        for i in range(self.heart_count):
            x = i * (HEART_SIZE + HEART_SPACING)
            heart_step = max(0, min(2, current_step - i * 2))  # 0 = empty, 1 = half, 2 = full
            if heart_step == 0:
                painter.setOpacity(HEART_DIMMED_OPACITY)
                painter.drawPixmap(x, 0, self._heart_pixmap)
            elif heart_step == 2:
                painter.setOpacity(1.0)
                painter.drawPixmap(x, 0, self._heart_pixmap)
            else:  # half -- dimmed full heart underneath, full-opacity heart clipped to the left half on top
                painter.setOpacity(HEART_DIMMED_OPACITY)
                painter.drawPixmap(x, 0, self._heart_pixmap)
                painter.setOpacity(1.0)
                painter.setClipRect(x, 0, HEART_SIZE // 2, HEART_SIZE)
                painter.drawPixmap(x, 0, self._heart_pixmap)
                painter.setClipping(False)
        painter.end()


def _bgr_to_pixmap(frame_bgr):
    frame_rgb = np.ascontiguousarray(frame_bgr[:, :, ::-1])  # BGR -> RGB, contiguous for QImage
    height, width, channels = frame_rgb.shape
    qimage = QImage(frame_rgb.data, width, height, channels * width, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimage)


class EngagementJudgmentWorker(QThread):
    """Runs one estimate_engagement_credit() call off the GUI thread --
    see LockdownWindow._run_engagement_judgment(). estimate_engagement_
    credit() itself never raises (fails open to ("full", None) on any
    error), so `done` is the only signal that ever actually fires."""
    done = pyqtSignal(str, object)  # (credit, revise_slide_or_None)

    def __init__(self, llm, window_seconds, distraction_events, distracted_seconds, slides_viewed, pace_status):
        super().__init__()
        self.llm = llm
        self.window_seconds = window_seconds
        self.distraction_events = distraction_events
        self.distracted_seconds = distracted_seconds
        self.slides_viewed = slides_viewed
        self.pace_status = pace_status

    def run(self):
        credit, revise_slide = self.llm.estimate_engagement_credit(
            self.window_seconds, self.distraction_events, self.distracted_seconds,
            self.slides_viewed, self.pace_status,
        )
        self.done.emit(credit, revise_slide)


class SessionReviewWorker(QThread):
    """Runs one generate_session_review() call off the GUI thread -- see
    LockdownWindow._on_material_completed(). Unlike EngagementJudgment
    Worker, generate_session_review() CAN raise (a genuinely broken
    review shouldn't be silently treated as a fine one), so both signals
    are real here."""
    done = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, llm, material_text, images_b64):
        super().__init__()
        self.llm = llm
        self.material_text = material_text
        self.images_b64 = images_b64

    def run(self):
        try:
            review = self.llm.generate_session_review(self.material_text, images_b64=self.images_b64)
            self.done.emit(review)
        except Exception as e:
            self.failed.emit(str(e))


class SessionReviewDialog(QDialog):
    """Shown once the material hits 100% (engagement-credited) completion,
    before lockdown actually releases -- a quick recap plus a few
    retention-check questions (see llm_client.generate_session_review()),
    NOT a graded test: no answer-checking, no requirement to type
    anything, just a "think about it, then reveal the answer" pass.
    "Continue" is the only way through -- there's no skip, since this is
    the one moment "you're done, take a second to review it" naturally
    fits, but it never blocks longer than it takes to actually read."""

    def __init__(self, recap, quiz, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Study Warden -- Session Review")
        self.resize(480, 420)

        layout = QVBoxLayout(self)
        title = QLabel("Nice work -- you've covered everything!")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(title)

        recap_label = QLabel(recap or "(no recap available)")
        recap_label.setWordWrap(True)
        layout.addWidget(recap_label)

        if quiz:
            quiz_heading = QLabel("Quick check -- think through these before moving on:")
            quiz_heading.setStyleSheet("font-weight: bold; margin-top: 8px;")
            layout.addWidget(quiz_heading)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            quiz_container = QWidget()
            quiz_layout = QVBoxLayout(quiz_container)
            for i, item in enumerate(quiz, start=1):
                self._add_quiz_item(quiz_layout, i, item.get("question", ""), item.get("answer", ""))
            quiz_layout.addStretch()
            scroll.setWidget(quiz_container)
            layout.addWidget(scroll, 1)

        continue_button = QPushButton("Continue")
        continue_button.clicked.connect(self.accept)
        layout.addWidget(continue_button)

    def _add_quiz_item(self, layout, index, question, answer):
        q_label = QLabel(f"{index}. {question}")
        q_label.setWordWrap(True)
        layout.addWidget(q_label)

        answer_label = QLabel(f"Answer: {answer}")
        answer_label.setWordWrap(True)
        answer_label.setStyleSheet(f"color: {theme.manager.colors['text_dim']}; padding-left: 12px;")
        answer_label.setVisible(False)
        layout.addWidget(answer_label)

        reveal_button = QPushButton("Show answer")

        def _reveal():
            answer_label.setVisible(True)
            reveal_button.setVisible(False)

        reveal_button.clicked.connect(_reveal)
        layout.addWidget(reveal_button)


class LockdownWindow(QWidget):
    exit_requested = pyqtSignal()
    material_completed = pyqtSignal()  # fires once, when engagement-credited progress hits 100% and the
                                        # session-review flow has resolved -- lets main_window.py mark the
                                        # matching Notion task done (see _on_material_completed())

    def __init__(self, pdf_path, timer_widget, total_slides=0, deadline_passed=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Study Warden -- Lockdown")
        self._timer_widget = timer_widget
        self._pdf_path = pdf_path  # kept for the 100%-completion session review, which needs to
                                    # re-extract text/images fresh (see _on_material_completed())
        self._deadline_passed = deadline_passed  # True if this session's course/field deadline already
                                                  # passed with the material not yet done -- shows a strong,
                                                  # STILL ESCAPABLE warning (see _build_deadline_banner());
                                                  # never blocks Esc/Ctrl+Shift+Alt/Exit Lockdown
        self._pdf_doc = None
        self._material_completed = False  # guards _on_material_completed() so it only ever fires once
        self._review_worker = None
        self._zoom_factor = 1.0  # multiplier on top of the fit-width baseline
        self._companion_worker = None
        self._page_labels = []          # rendered page QLabels in order -- used to find which one is actually
                                         # on screen right now (see _current_visible_page())
        self._total_slides_override = total_slides  # exact slide count the caller's time estimate was based on
                                                      # (e.g. Revision's "covered up to slide N") -- 0 means "not
                                                      # given", fall back to the PDF's real page count once loaded
        self._effective_total_slides = 0             # resolved once the PDF is open -- see _load_pdf()
        self._pace_frozen = False                     # True while a pose challenge/push-ups is up -- see
                                                        # _set_pace_frozen(), driven straight off the same
                                                        # monitor signal that pauses the study countdown itself
        self._disruption_started_at = None            # wall-clock time the current disruption began, for
                                                        # tallying _window_distracted_seconds once it ends

        # Engagement judgment: every ENGAGEMENT_WINDOW_MS, a short summary
        # of what happened in that window (distraction count/duration,
        # which slides were viewed) goes to the LLM (text-only, fast --
        # see llm_client.estimate_engagement_credit()), which returns how
        # much of that window should actually count toward progress. The
        # pacing bar's fill is driven by _credited_progress_seconds (a
        # SEPARATE running total from the real timer's study_seconds_
        # elapsed()) rather than raw elapsed time, so a distracted window
        # can fall behind real time -- the real countdown/block timing
        # itself is completely untouched by any of this.
        self.llm = LLMClient()
        self._credited_progress_seconds = 0.0
        self._credit_multiplier = 1.0
        self._window_distraction_count = 0
        self._window_distracted_seconds = 0.0
        self._window_slides_viewed = set()
        self._engagement_worker = None
        self._last_pace_status = "unknown"

        # SESSION-WIDE totals (never reset, unlike the per-window
        # counters above) -- purely for engagement_log.py's history once
        # this session ends (see main_window.py's _close_lockdown(),
        # which reads get_engagement_summary()). Previously this data
        # only ever existed transiently per-90s-window, driving the
        # pacing bar in the moment, then thrown away -- nothing
        # persisted across a whole session, let alone across sessions.
        self._session_distraction_events = 0
        self._session_distracted_seconds = 0.0
        self._session_credit_counts = {"full": 0, "reduced": 0, "none": 0}

        # "Study Warden -- " (see overlay.py) also allow-lists the break-
        # choice/pocket-wait/mini-game windows that open on top of lockdown
        # during a break -- without this, the enforcer (which only knows
        # this window's own hwnd) would minimize them the instant its next
        # poll ran, since it has no idea they're part of the same app.
        # "Snipping Tool" -- Windows' own built-in screenshot tool (also
        # what Win+Shift+S opens as of current Windows versions) -- was
        # getting minimized like anything else within one poll interval,
        # making it look like screenshots just didn't work during lockdown.
        self._enforcer = lockdown_os.LockdownEnforcer(
            get_allowed_hwnds=lambda: {int(self.winId())},
            allowed_title_substrings=("Discord", "Spotify", "Study Warden -- ", "Snipping Tool"),
        )
        self._exit_hotkey = lockdown_os.ExitHotkeyDetector(self)
        self._exit_hotkey.triggered.connect(self.exit_requested.emit)

        # Docked "Notepad" and "YouTube" panels -- both permanently visible
        # in the right sidebar (built below in body_row), not floating
        # overlays/toggles the way they used to be. Built here, before that
        # layout exists, only because neither depends on anything built
        # later.
        self.notepad = ScratchpadPanel(self, closable=False, title="Notepad")

        # Always-on video player -- user-confirmed to run DURING lockdown
        # itself (see attention_video.py's module docstring for why that's
        # a deliberate exception here). Docked in the sidebar below the
        # notepad. Channel is whatever's saved in Settings' "Attention
        # Video" section, falling back to AttentionVideoPlayer's own
        # DEFAULT_CHANNEL_HANDLE if nothing's been saved.
        from settings_dialog import SETTINGS_ORG, SETTINGS_APP, KEY_YOUTUBE_CHANNEL_HANDLE
        saved_handle = QSettings(SETTINGS_ORG, SETTINGS_APP).value(KEY_YOUTUBE_CHANNEL_HANDLE)
        if saved_handle:
            self.attention_video = AttentionVideoPlayer(channel_handle=f"@{saved_handle}", parent=self)
        else:
            self.attention_video = AttentionVideoPlayer(parent=self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)

        # ---- header: Webcam | Timer | Voice assistant | Time & Exit ----
        header_row = QHBoxLayout()

        # Every zone below is added with AlignVCenter -- without it, a
        # zone shorter than the row's tallest sibling (webcam_label's
        # fixed 240px) gets STRETCHED to fill that height instead of
        # sitting centered in it, which is exactly what made the clock
        # pill balloon into a huge block (see _apply_clock_theme() below)
        # rather than a small, homepage-sized label.
        V = Qt.AlignmentFlag.AlignVCenter

        self.webcam_label = QLabel()
        self.webcam_label.setFixedSize(*WEBCAM_LABEL_SIZE)
        self.webcam_label.setStyleSheet("background-color: #000;")
        self.webcam_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_row.addWidget(self.webcam_label, alignment=V)
        header_row.addSpacing(10)

        # Pose-challenge feed (see vision/pose_engine.py) -- right next
        # to the webcam per the user's own wireframe, since it's showing
        # a live view of the SAME camera feed during a challenge, not an
        # unrelated zone. Still hidden outside an active challenge (see
        # _on_pose_frame() below) -- Qt collapses a hidden widget's
        # space in a QHBoxLayout automatically, so this costs nothing
        # when idle.
        self.pose_label = QLabel()
        self.pose_label.setFixedSize(*POSE_LABEL_SIZE)
        self.pose_label.setStyleSheet("background-color: #000;")
        self.pose_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pose_label.setVisible(False)
        header_row.addWidget(self.pose_label, alignment=V)
        header_row.addSpacing(10)

        # Spotify "now playing" (see spotify_widget.py) -- reads straight
        # from Windows' own media session info, no Spotify API key/OAuth.
        self.spotify_widget = SpotifyNowPlayingWidget(parent=self)
        header_row.addWidget(self.spotify_widget, alignment=V)

        # This is the ONE place leftover width goes (see the "no
        # trailing stretch" note by time_exit_column below) -- it reads
        # as "camera + music" left-packed, then whatever's left over,
        # then "timer | voice assistant | clock/exit" pinned flush to
        # the right edge as a unit, per the user's own wireframe. An
        # EARLIER version of this comment warned a stretch here would
        # strand a single widget with dead gaps on both sides -- that
        # concern doesn't apply to a whole trailing group like this one.
        header_row.addStretch()
        header_row.addSpacing(16)
        header_row.addWidget(_vline())
        header_row.addSpacing(16)

        # Study timer, right beside the voice assistant -- per the
        # user's own wireframe. NOT a custom expand/collapse of my own:
        # main_window.py already wraps time_label in a HoverRevealPanel
        # (ui/theme.py) -- a small "▾" peek hint that expands the big
        # countdown on hover -- specifically so that behavior "travels
        # with the widget" when it's reparented in here (see that
        # file's own comment on the wrapping). An EARLIER version of
        # this code didn't know that existed and built a second,
        # competing click-toggle + hid time_label directly, which is
        # exactly what produced two visibly different "reveal the
        # timer" controls at once. Embedding self._timer_widget as-is
        # already brings the peek hint + countdown + status text with
        # it, correctly stacked, with no extra code needed here.
        if self._timer_widget is not None:
            self._timer_widget.setParent(self)
            self._timer_widget.start_button.setStyleSheet("font-size: 20px; padding: 10px 16px; font-weight: bold;")
            header_row.addWidget(self._timer_widget, alignment=V)
            header_row.addSpacing(16)

        # Voice assistant status (see voice_assistant_panel.py) sits right
        # beside the clock -- user-requested placement, right next to the
        # widget it's most relevant to (you're asking about whatever's on
        # screen *now*, the same moment you're watching the timer).
        self.voice_assistant = VoiceAssistantPanel(self.llm, self._current_slide_text, parent=self)
        header_row.addWidget(self.voice_assistant, alignment=V)
        header_row.addSpacing(16)
        header_row.addWidget(_vline())
        header_row.addSpacing(16)

        # Wall-clock (time of day) + Exit Lockdown, grouped in their own
        # column at the far right. Exit isn't part of the sketch this
        # header's structure is based on, but it's this app's one
        # non-negotiable control (see the module docstring's "always
        # escapable" note) -- it keeps a clearly visible, dedicated spot
        # rather than being dropped for it.
        time_exit_column = QVBoxLayout()
        time_exit_column.addStretch()
        self.clock_label = QLabel("")
        self.clock_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Same accent-pill treatment as the homepage's clock (see
        # ui/theme.py's HeaderBanner._apply_colors()) -- inverted text on
        # an accent-colored background, not just a plain label, and
        # live-updates on a theme switch the same way HeaderBanner's does.
        # setFixedHeight() keeps it a tight pill regardless of whatever
        # extra vertical space this column ends up with -- the addStretch()
        # calls around it absorb that space instead of the label itself.
        self.clock_label.setFixedHeight(26)
        self._apply_clock_theme(theme.manager.colors)
        theme.manager.changed.connect(self._on_clock_theme_changed)
        time_exit_column.addWidget(self.clock_label)
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start(1000)
        self._update_clock()

        self.exit_button = QPushButton("Exit Lockdown (Esc / Ctrl+Shift+Alt)")
        self.exit_button.clicked.connect(self._on_exit_clicked)
        time_exit_column.addWidget(self.exit_button)
        time_exit_column.addStretch()
        header_row.addLayout(time_exit_column)
        # No trailing stretch here -- the one right after the Spotify
        # widget (above) is the only one now, so it's the one place
        # leftover width goes. Voice assistant + clock/exit end up
        # pushed flush to the right edge as a unit instead, per the
        # user's own wireframe -- a SECOND stretch here would split the
        # leftover space and center that unit instead of pinning it to
        # the edge.

        outer.addLayout(header_row)
        outer.addWidget(_hline())

        # A strong, hard-to-miss nudge -- NOT a block -- for when this
        # course/field's deadline has already passed with the material
        # still unfinished. Esc/Ctrl+Shift+Alt/Exit Lockdown all still
        # work exactly as always; this is deliberately just a loud
        # reminder, per the same "always escapable" principle every other
        # lock in this app follows. Hidden entirely unless deadline_
        # passed was actually true at construction time; hidden again
        # once the material's own 100%-completion flow resolves (see
        # _finish_material_completion()).
        self.deadline_banner = QLabel(
            "⚠ The deadline for this material has already passed and it isn't finished yet."
        )
        self.deadline_banner.setWordWrap(True)
        self.deadline_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.deadline_banner.setStyleSheet(
            f"background-color: #DC3545; color: #FFFFFF; font-weight: bold; font-size: 13px; "
            f"padding: 10px; border-radius: {theme.RADIUS_TAB}px;"
        )
        self.deadline_banner.setVisible(self._deadline_passed)
        outer.addWidget(self.deadline_banner)

        # ---- body: left (tools + slides) | right (Notepad + YouTube) ----
        body_row = QHBoxLayout()
        left_column = QVBoxLayout()

        # "tools": annotation toolbar (draws directly on the material --
        # see annotation_overlay.py, pages are flat rendered images so
        # "highlighting" is a translucent freehand marker, not text-aware
        # selection) + pacing (heart icons) + zoom, one consolidated row
        # directly above the slides rather than several stacked control
        # rows. The canvas itself gets parented onto pdf_container once
        # that's built below (needs its real size to position against);
        # the toolbar only needs a reference to it.
        self.pdf_annotation_canvas = DrawingCanvas(background_color=None)
        tools_row = QHBoxLayout()
        self.annotation_toolbar = AnnotationToolbar(self.pdf_annotation_canvas)
        tools_row.addWidget(self.annotation_toolbar)
        tools_row.addSpacing(20)

        # Pacing: how far you SHOULD be by now (elapsed study time / the
        # session's total estimate, times the slide count) vs which page
        # you're ACTUALLY scrolled to right now -- a row of heart icons
        # instead of a plain bar (see HeartBar), filled left to right in
        # half-heart steps. Hidden entirely when there's no slide count to
        # pace against (no material, or a 0-page PDF).
        self.pace_bar = HeartBar()
        tools_row.addWidget(self.pace_bar)
        self.pace_label = QLabel("")
        tools_row.addWidget(self.pace_label)
        self._set_pace_visible(False)

        self._pace_timer = QTimer(self)
        self._pace_timer.timeout.connect(self._update_pace)

        self._engagement_timer = QTimer(self)
        self._engagement_timer.timeout.connect(self._run_engagement_judgment)

        tools_row.addStretch()

        self.zoom_out_button = QPushButton("Zoom -")
        self.zoom_out_button.clicked.connect(self._on_zoom_out)
        tools_row.addWidget(self.zoom_out_button)
        self.zoom_label = QLabel("100%")
        tools_row.addWidget(self.zoom_label)
        self.zoom_in_button = QPushButton("Zoom +")
        self.zoom_in_button.clicked.connect(self._on_zoom_in)
        tools_row.addWidget(self.zoom_in_button)

        left_column.addLayout(tools_row)

        # A soft nudge, not a block -- only shown when the periodic
        # engagement judgment actually flags a slide worth a second look
        # (see _on_engagement_judged()). Hidden the rest of the time,
        # same pattern as companion_status_label below.
        revise_row = QHBoxLayout()
        self.revise_banner = QLabel("")
        self.revise_banner.setWordWrap(True)
        self.revise_banner.setStyleSheet(
            f"background-color: #FFF3CD; color: #664D03; padding: 10px; "
            f"border: 1px solid #FFECB5; border-radius: {theme.RADIUS_TAB}px;"
        )
        self.revise_banner.setVisible(False)
        revise_row.addWidget(self.revise_banner, 1)
        self.revise_dismiss_button = QPushButton("Dismiss")
        self.revise_dismiss_button.clicked.connect(self._dismiss_revise_banner)
        self.revise_dismiss_button.setVisible(False)
        revise_row.addWidget(self.revise_dismiss_button)
        left_column.addLayout(revise_row)

        # Every page gets rendered as its own QLabel, stacked vertically in
        # this container -- the whole document scrolls continuously as one
        # long strip rather than one page at a time with Prev/Next buttons.
        self.pdf_container = QWidget()
        self.pdf_container_layout = QVBoxLayout(self.pdf_container)
        self.pdf_container_layout.setContentsMargins(0, 0, 0, 0)
        self.pdf_container_layout.setSpacing(8)

        self.pdf_scroll = QScrollArea()
        self.pdf_scroll.setWidgetResizable(True)
        self.pdf_scroll.setWidget(self.pdf_container)
        self.pdf_scroll.viewport().installEventFilter(self)  # Ctrl+wheel zoom
        left_column.addWidget(self.pdf_scroll, 1)

        self._pdf_opacity_effect = QGraphicsOpacityEffect(self.pdf_scroll)
        self._pdf_opacity_effect.setOpacity(1.0)
        self.pdf_scroll.setGraphicsEffect(self._pdf_opacity_effect)

        # Now that pdf_container actually exists, park the annotation
        # canvas on top of it as a real child -- being a child (not just
        # visually overlapping) means it scrolls WITH the content
        # automatically, no manual scroll-offset syncing needed. Kept in
        # sync with the container's real size via eventFilter() below,
        # since zoom/resize changes it (and setWidgetResizable(True)
        # reflows the container on its own schedule, not synchronously
        # with when _render_all_pages() runs).
        self.pdf_annotation_canvas.setParent(self.pdf_container)
        self.pdf_annotation_canvas.resize(self.pdf_container.size())
        self.pdf_annotation_canvas.move(0, 0)
        self.pdf_annotation_canvas.raise_()
        self.pdf_container.installEventFilter(self)

        self.companion_status_label = QLabel("")
        self.companion_status_label.setWordWrap(True)
        self.companion_status_label.setVisible(False)
        left_column.addWidget(self.companion_status_label)

        body_row.addLayout(left_column, 1)
        body_row.addWidget(_vline())

        # Right sidebar: Notepad (stretches to fill whatever's left) above
        # YouTube (stays at the video player's own fixed size) -- both
        # permanently docked, not toggled overlays.
        right_sidebar = QWidget()
        right_column = QVBoxLayout(right_sidebar)
        right_column.setContentsMargins(8, 0, 0, 0)
        right_sidebar.setFixedWidth(SIDEBAR_WIDTH)

        right_column.addWidget(self.notepad, 1)
        right_column.addWidget(_hline())
        youtube_title = QLabel("YouTube")
        youtube_title.setStyleSheet("font-weight: bold; font-size: 14px;")
        right_column.addWidget(youtube_title)
        right_column.addWidget(self.attention_video, alignment=Qt.AlignmentFlag.AlignHCenter)

        body_row.addWidget(right_sidebar)
        outer.addLayout(body_row, 1)

        self._load_pdf(pdf_path)

    # ---- PDF viewing ----

    def _load_pdf(self, pdf_path):
        if not pdf_path:
            self._show_message(
                "No study material found for today.\n\n"
                "Attach a PDF to today's course in the Study Planner and it'll show up here "
                "automatically next time."
            )
            return
        try:
            self._pdf_doc = fitz.open(pdf_path)
        except Exception as e:
            self._show_message(f"Couldn't open today's material ({pdf_path}):\n{e}")
            return
        self._effective_total_slides = self._total_slides_override or len(self._pdf_doc)
        self._set_pace_visible(self._effective_total_slides > 0)
        # Deferred rather than called directly: at construction time (before
        # this widget has actually been shown), the scroll area's viewport
        # hasn't been laid out yet and reports a stale/default width, which
        # would make the very first fit-width render come out undersized.
        # By the next event-loop iteration (still before the caller gets a
        # chance to do anything else), it has real geometry.
        QTimer.singleShot(0, self._render_all_pages)

    def _clear_pdf_container(self):
        self._page_labels = []
        while self.pdf_container_layout.count():
            item = self.pdf_container_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _show_message(self, text):
        self._clear_pdf_container()
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        self.pdf_container_layout.addWidget(label)

    def _render_all_pages(self):
        if self._pdf_doc is None:
            return
        self._clear_pdf_container()
        # Fit-width * zoom factor: fit-width is the baseline (each page
        # exactly as wide as the viewport at 100%), _zoom_factor scales up
        # or down from there -- see _on_zoom_in/_on_zoom_out.
        viewport_width = max(self.pdf_scroll.viewport().width(), 200)
        page_count = len(self._pdf_doc)
        render_count = min(page_count, MAX_RENDERED_PAGES)
        for i in range(render_count):
            page = self._pdf_doc[i]
            page_width_pt = page.rect.width or 1
            zoom = max(viewport_width / page_width_pt, 0.1) * self._zoom_factor
            rendered = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            image = QImage(rendered.samples, rendered.width, rendered.height,
                            rendered.stride, QImage.Format.Format_RGB888)
            page_label = QLabel()
            page_label.setPixmap(QPixmap.fromImage(image))
            page_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            self.pdf_container_layout.addWidget(page_label)
            self._page_labels.append(page_label)
        if render_count < page_count:
            note = QLabel(f"(showing the first {render_count} of {page_count} pages)")
            note.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.pdf_container_layout.addWidget(note)

        # The annotation canvas is kept in sync with the container's real
        # size via eventFilter()'s Resize handling above, not here --
        # this layout change doesn't take visible effect synchronously.

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._pdf_doc is not None:
            self._render_all_pages()  # re-scale every page to the new width

    # ---- header clock ----

    def _update_clock(self):
        self.clock_label.setText(datetime.now().strftime("%I:%M:%S %p"))

    def _apply_clock_theme(self, colors):
        self.clock_label.setStyleSheet(
            f"font-size: 15px; font-weight: bold; color: {colors['bg']}; "
            f"background-color: {colors['accent']}; padding: 3px 10px; border-radius: 3px;"
        )

    def _on_clock_theme_changed(self, theme_id, colors):
        self._apply_clock_theme(colors)

    # ---- zoom ----

    def _on_zoom_in(self):
        self._set_zoom(self._zoom_factor * ZOOM_STEP)

    def _on_zoom_out(self):
        self._set_zoom(self._zoom_factor / ZOOM_STEP)

    def _set_zoom(self, factor):
        self._zoom_factor = max(MIN_ZOOM, min(MAX_ZOOM, factor))
        self.zoom_label.setText(f"{round(self._zoom_factor * 100)}%")
        if self._pdf_doc is not None:
            self._render_all_pages()

    def eventFilter(self, obj, event):
        if obj is self.pdf_scroll.viewport() and event.type() == QEvent.Type.Wheel:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                if event.angleDelta().y() > 0:
                    self._on_zoom_in()
                else:
                    self._on_zoom_out()
                return True
        if obj is self.pdf_container and event.type() == QEvent.Type.Resize:
            # React to the container's REAL final size whenever Qt actually
            # applies it, rather than guessing how many event-loop turns
            # _render_all_pages()'s layout changes take to settle inside the
            # QScrollArea (setWidgetResizable(True) reflows this on its own
            # schedule, not necessarily synchronously with adjustSize()).
            self.pdf_annotation_canvas.resize(self.pdf_container.size())
            self.pdf_annotation_canvas.raise_()
        return super().eventFilter(obj, event)

    # ---- pacing progress bar ----

    def _set_pace_visible(self, visible):
        self.pace_bar.setVisible(visible)
        self.pace_label.setVisible(visible)

    def _current_visible_page(self):
        """0-based index of whichever rendered page is centered in the
        viewport right now, via scroll position -- the pacing bar's
        "actual" side. None if nothing's rendered yet (still loading, or
        no material)."""
        if not self._page_labels:
            return None
        scrollbar = self.pdf_scroll.verticalScrollBar()
        viewport_center_y = scrollbar.value() + self.pdf_scroll.viewport().height() // 2
        for i, label in enumerate(self._page_labels):
            top = label.y()
            if top <= viewport_center_y < top + label.height():
                return i
        return 0 if viewport_center_y < self._page_labels[0].y() else len(self._page_labels) - 1

    def _update_pace(self):
        """Recomputes and redraws the pacing bar: how far into the total
        estimated study time you are (the bar's fill + "expected slide"),
        against which slide you're actually scrolled to right now. Runs on
        a timer (see start_lockdown()/stop_lockdown()) rather than only on
        scroll, since the EXPECTED side keeps moving even while you hold
        still reading a page.

        The bar's fill is driven by _credited_progress_seconds, NOT the
        timer's real study_seconds_elapsed() -- they start equal, but can
        diverge once the periodic engagement judgment reduces
        _credit_multiplier for a distracted window (see
        _run_engagement_judgment()/_on_engagement_judged()). This only
        ever affects the PACING VISUALIZATION -- the real countdown and
        block transitions are governed entirely by the timer's own state,
        untouched by any of this. Only accrues while actually studying
        (mirrors study_seconds_elapsed()'s own phase-awareness) -- this
        method simply doesn't run at all while _pace_frozen (the timer's
        stopped then), so an active pose challenge is already excluded
        without a separate check here."""
        if self._effective_total_slides <= 0 or self._timer_widget is None:
            return
        total_seconds = self._timer_widget.total_study_seconds()
        if total_seconds <= 0:
            return

        if self._timer_widget.phase == "study":
            self._credited_progress_seconds += (PACE_UPDATE_INTERVAL_MS / 1000) * self._credit_multiplier
        fraction = min(1.0, self._credited_progress_seconds / total_seconds)
        expected_slide = max(1, min(self._effective_total_slides, round(fraction * self._effective_total_slides)))
        self.pace_bar.set_fraction(fraction)

        # Genuinely 100% (not just clamped for display) -- the material's
        # covered well before the allotted time necessarily ran out.
        # Checked against the UNCLAMPED total, not `fraction`, since
        # `fraction` is capped at 1.0 the instant credited progress
        # reaches the total and would otherwise re-trigger on every
        # subsequent tick without the _material_completed guard alone.
        if not self._material_completed and self._credited_progress_seconds >= total_seconds:
            self._on_material_completed()
            return

        actual_index = self._current_visible_page()
        if actual_index is None:
            self._last_pace_status = "unknown"
            self.pace_label.setText(f"Pace: slide {expected_slide}/{self._effective_total_slides}")
            return

        actual_slide = actual_index + 1
        self._window_slides_viewed.add(actual_slide)
        diff = actual_slide - expected_slide
        if diff > PACE_TOLERANCE_SLIDES:
            status = f"{diff} ahead"
        elif diff < -PACE_TOLERANCE_SLIDES:
            status = f"{-diff} behind"
        else:
            status = "on pace"  # within tolerance -- including small rounding gaps, not just an exact match
        self._last_pace_status = status
        self.pace_label.setText(f"Slide {actual_slide}/{self._effective_total_slides} · pace {expected_slide} ({status})")

    def _current_slide_text(self):
        """Text of whichever slide is currently scrolled into view (see
        _current_visible_page()) -- the voice assistant's Q&A context
        (see voice_assistant_panel.py). "" if there's no PDF loaded or
        nothing rendered yet, which VoiceAssistantPanel treats as
        "answer from general knowledge" rather than an error."""
        if self._pdf_doc is None:
            return ""
        index = self._current_visible_page()
        if index is None or not (0 <= index < len(self._pdf_doc)):
            return ""
        return self._pdf_doc[index].get_text()

    def _set_pace_frozen(self, frozen):
        """Freezes the pacing bar (both the expected-pace side and the
        actual-scroll-position side) for as long as a pose challenge/
        push-ups is up, so falling "behind" during a required break from
        reading never gets held against you -- resumes exactly where it
        left off once the challenge clears. Driven by the same monitor
        signal (see update_frame()'s is_challenge) that already pauses the
        study countdown itself via pause_for_distraction() -- one real
        source of truth for "is something disrupting study right now",
        not a second guess at it."""
        if frozen == self._pace_frozen:
            return
        self._pace_frozen = frozen
        if frozen:
            self._disruption_started_at = time.time()
            self._update_pace()  # one last real, up-to-date snapshot before freezing -- never freeze on stale/empty text
            self._pace_timer.stop()
            if not self.pace_label.isHidden() and self.pace_label.text():
                self.pace_label.setText(self.pace_label.text() + " · paused")
        else:
            if self._disruption_started_at is not None:
                disruption_seconds = time.time() - self._disruption_started_at
                self._window_distraction_count += 1
                self._window_distracted_seconds += disruption_seconds
                self._session_distraction_events += 1
                self._session_distracted_seconds += disruption_seconds
                self._disruption_started_at = None
            if self._effective_total_slides > 0:
                self._pace_timer.start(PACE_UPDATE_INTERVAL_MS)

    def _run_engagement_judgment(self):
        """Every ENGAGEMENT_WINDOW_MS (see start_lockdown()/stop_
        lockdown()), sends a short summary of what actually happened in
        that window -- distraction count/duration, which slides were
        viewed, current pace status -- to the LLM for a fast, text-only
        credit judgment (see llm_client.estimate_engagement_credit()).
        Skipped (this window's data just carries over, uncounted, into
        the next tick) if nothing happened yet, a previous judgment call
        is still running, or a voice assistant question is actively in
        flight (see VoiceAssistantPanel.is_idle) -- a background
        housekeeping call has no business competing with the LLM's
        attention while the user is actively waiting on a spoken answer.
        Harmless to skip: estimate_engagement_credit() already fails
        open to full credit on any error/timeout, so a deferred window
        is no different from one that happened to time out."""
        if self._timer_widget is None or self._timer_widget.phase != "study":
            return
        if self._engagement_worker is not None and self._engagement_worker.isRunning():
            return
        if not self.voice_assistant.is_idle:
            return
        if not self._window_slides_viewed:
            return  # nothing to judge yet (e.g. right at session start)

        window_seconds = ENGAGEMENT_WINDOW_MS // 1000
        distraction_events = self._window_distraction_count
        distracted_seconds = round(self._window_distracted_seconds, 1)
        slides_viewed = sorted(self._window_slides_viewed)
        pace_status = self._last_pace_status

        self._window_distraction_count = 0
        self._window_distracted_seconds = 0.0
        self._window_slides_viewed = set()

        self._engagement_worker = EngagementJudgmentWorker(
            self.llm, window_seconds, distraction_events, distracted_seconds, slides_viewed, pace_status
        )
        self._engagement_worker.done.connect(self._on_engagement_judged)
        self._engagement_worker.start()

    def _on_engagement_judged(self, credit, revise_slide):
        self._credit_multiplier = CREDIT_MULTIPLIERS.get(credit, 1.0)
        if credit in self._session_credit_counts:
            self._session_credit_counts[credit] += 1
        if revise_slide is not None:
            self.revise_banner.setText(
                f"Consider revising slide {revise_slide} -- recent engagement suggests it might need another look."
            )
            self.revise_banner.setVisible(True)
            self.revise_dismiss_button.setVisible(True)

    def _dismiss_revise_banner(self):
        self.revise_banner.setVisible(False)
        self.revise_dismiss_button.setVisible(False)

    def get_engagement_summary(self):
        """Session-wide engagement totals accumulated so far (see the
        _session_* counters in __init__) -- read by main_window.py's
        _close_lockdown() right before this window is discarded, to
        persist one row to core/engagement_log.py's local history.
        Safe to call at any point in the session, not just at the very
        end (a mid-session exit still has real partial data worth
        logging, same as session_snapshot.py's debt already does for
        an incomplete session)."""
        studied_seconds = self._timer_widget.study_seconds_elapsed() if self._timer_widget is not None else 0
        return {
            "distraction_events": self._session_distraction_events,
            "distracted_seconds": self._session_distracted_seconds,
            "studied_seconds": studied_seconds,
            "credit_counts": dict(self._session_credit_counts),
        }

    def _on_material_completed(self):
        """Fires once, the first time engagement-credited progress
        genuinely reaches 100% of the total estimated study time --
        possibly well before the allotted time would otherwise run out.
        Extracts the material fresh and asks the LLM for a quick recap +
        retention-check quiz (see llm_client.generate_session_review());
        on ANY failure (extraction or the LLM call), skips straight to
        finishing rather than getting stuck -- the review is a bonus on
        top of finishing early, not a gate blocking it."""
        if self._material_completed:
            return
        self._material_completed = True
        self._pace_timer.stop()  # the material's done -- no more pace ticking needed

        try:
            import syllabus_parser
            text, images = syllabus_parser.extract_text_and_images(self._pdf_path, max_images=6)
        except Exception as e:
            print(f"[LockdownWindow] couldn't extract material for the session review, skipping straight to exit: {e}")
            self._finish_material_completion()
            return

        self._review_worker = SessionReviewWorker(self.llm, text, images)
        self._review_worker.done.connect(self._on_review_ready)
        self._review_worker.failed.connect(self._on_review_failed)
        self._review_worker.start()

    def _on_review_ready(self, review):
        dialog = SessionReviewDialog(review.get("recap", ""), review.get("quiz", []), parent=self)
        dialog.exec()
        self._finish_material_completion()

    def _on_review_failed(self, message):
        print(f"[LockdownWindow] session review failed, skipping straight to exit: {message}")
        self._finish_material_completion()

    def _finish_material_completion(self):
        """Finishes the current study block early (as if its timer had
        naturally reached zero -- see state_machine.finish_block_early())
        and tells main_window.py the material's done, so it can mark the
        matching Notion task complete. The real countdown/block-
        transition logic is entirely state_machine's own, untouched by
        any of this -- this just triggers it earlier than a natural
        zero would have."""
        self.deadline_banner.setVisible(False)  # the material's done -- nothing left to warn about
        self.material_completed.emit()
        self._timer_widget.finish_block_early()

    # ---- webcam feed ----

    def update_frame(self, main_frame_bgr, pose_frame_bgr):
        pixmap = _bgr_to_pixmap(main_frame_bgr)
        self.webcam_label.setPixmap(pixmap.scaled(
            self.webcam_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation
        ))
        is_challenge = pose_frame_bgr is not None
        if is_challenge:
            pose_pixmap = _bgr_to_pixmap(pose_frame_bgr)
            self.pose_label.setPixmap(pose_pixmap.scaled(
                self.pose_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation
            ))
        self.pose_label.setVisible(is_challenge)
        self.set_dimmed(is_challenge)
        self._set_pace_frozen(is_challenge)

    def set_dimmed(self, dimmed):
        """Fades the PDF while a pose challenge is up -- you should be
        looking at the camera to match the pose, not reading."""
        self._pdf_opacity_effect.setOpacity(DIMMED_OPACITY if dimmed else 1.0)

    # ---- going live / standing down ----

    def start_lockdown(self):
        """Positions this window full-screen on monitor 1, kicks off
        Discord/Spotify launch+tiling on monitor 2 (off the GUI thread --
        it can block briefly waiting for a freshly-launched app's window to
        appear), and starts the minimize-everything-else enforcement loop
        plus the Ctrl+Shift+Alt exit detector. Call once, right after
        construction and before/instead of a plain show()."""
        geometry = lockdown_os.screen_geometry(LOCKDOWN_MONITOR)
        if geometry is not None:
            self.setGeometry(geometry)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        # Fade in rather than snapping straight to full-screen -- this is
        # the highest-stakes transition in the app (going into an enforced
        # session), so it gets the one deliberate entrance treatment.
        fade_effect = QGraphicsOpacityEffect(self)
        fade_effect.setOpacity(0.0)
        self.setGraphicsEffect(fade_effect)
        self.showFullScreen()

        self._entrance_animation = QPropertyAnimation(fade_effect, b"opacity", self)
        self._entrance_animation.setDuration(FADE_MS)
        self._entrance_animation.setStartValue(0.0)
        self._entrance_animation.setEndValue(1.0)
        self._entrance_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        # Drop the effect once the fade finishes -- leaving a
        # QGraphicsOpacityEffect attached for the whole session adds a
        # compositing cost with nothing left to show for it.
        self._entrance_animation.finished.connect(lambda: self.setGraphicsEffect(None))
        self._entrance_animation.start()

        self._companion_worker = lockdown_os.CompanionAppsWorker(monitor_index=COMPANION_APPS_MONITOR)
        self._companion_worker.done.connect(self._on_companion_apps_done)
        self._companion_worker.start()

        self._enforcer.start()
        self._exit_hotkey.start()
        self._pace_timer.start(PACE_UPDATE_INTERVAL_MS)
        self._engagement_timer.start(ENGAGEMENT_WINDOW_MS)

    def _on_companion_apps_done(self, note):
        if note:
            self.companion_status_label.setText(f"Discord/Spotify: {note}")
            self.companion_status_label.setVisible(True)
        else:
            self.companion_status_label.setVisible(False)

    def stop_lockdown(self):
        self._enforcer.stop()
        self._exit_hotkey.stop()
        self._pace_timer.stop()
        self._engagement_timer.stop()
        self.voice_assistant.shutdown()
        self.spotify_widget.shutdown()
        if self._companion_worker is not None and self._companion_worker.isRunning():
            self._companion_worker.wait(100)  # best-effort; don't hang the UI on the way out
        self._companion_worker = None

    def animate_close(self):
        """Fades the (by now empty-shell -- call take_timer_widget() first)
        window out, then actually closes it once the fade finishes. The
        timer widget itself should already be detached before this runs so
        it lands in its new home immediately rather than fading with the
        rest of the shell."""
        fade_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(fade_effect)
        self._exit_animation = QPropertyAnimation(fade_effect, b"opacity", self)
        self._exit_animation.setDuration(FADE_MS)
        self._exit_animation.setStartValue(1.0)
        self._exit_animation.setEndValue(0.0)
        self._exit_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._exit_animation.finished.connect(self.close)
        self._exit_animation.start()

    # ---- exit ----

    def _on_exit_clicked(self):
        self.exit_requested.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.exit_requested.emit()
        else:
            super().keyPressEvent(event)

    def take_timer_widget(self):
        """Detaches the re-parented timer widget so the caller can put it
        back where it came from, and stops the lockdown enforcement/hotkey
        polling -- call this before closing the window."""
        self.stop_lockdown()
        if self._timer_widget is not None:
            self._timer_widget.setParent(None)
        return self._timer_widget
