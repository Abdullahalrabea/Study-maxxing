# The Revision panel: upload a slide deck (PDF), say which slide you
# actually covered up to in class, and get an LLM estimate of how many
# minutes are needed to FULLY REVISE (not learn for the first time)
# everything up to that point -- see llm_client.estimate_revision_minutes()
# for why that's a different estimate than the Study Planner's "learning
# new material" one. The estimate always lands in an editable box, never
# starts a session by itself -- review/adjust it, then Start Revision
# Session launches the exact same Monitor Lockdown session Focus Session
# does, just with this slide deck and this total time instead of
# auto-picked-from-schedule material and whatever's in Focus Session's own
# total-minutes field.
#
# No Notion involvement at all -- unlike the Study Planner/Schedule
# Assistant, this never reads or writes anything in your Notion workspace,
# it only launches a local monitored session.
#
# HOW TO EMBED: plain QWidget, then connect session_requested to whatever
# should actually start the session (see ui/main_window.py, which connects
# it to FocusSessionWidget.start_custom_session()):
#     from revision_panel import RevisionPanel
#     panel = RevisionPanel()
#     panel.session_requested.connect(focus_widget.start_custom_session)

from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QLabel, QPushButton, QSpinBox, QFileDialog, QMessageBox,
)
from PyQt6.QtCore import QThread, pyqtSignal

import syllabus_parser
from syllabus_parser import SyllabusParseError
from llm_client import LLMClient, LLMClientError
from theme import Card

REVISION_ERRORS = (SyllabusParseError, LLMClientError)

# Shown as this page's header help icon (see theme.CollapsibleSection's
# help_text param, wired in ui/main_window.py) rather than as a permanent
# inline label -- "how does this page work" text.
REVISION_HELP_TEXT = (
    "Upload your lecture slides (PDF), then say which slide you covered in class up to. It "
    "estimates how many minutes you need to FULLY revise everything from slide 1 up to that "
    "point -- this is revision, not learning it fresh, so it's a faster pace than the Study "
    "Planner's exam-prep estimate. Adjust the number by hand if you'd like, then Start Revision "
    "Session launches the same Monitor Lockdown session Focus Session does, using this slide deck "
    "and this total time -- split into blocks (max 50 min each, 10-minute breaks between them) the "
    "same way any session is."
)

MAX_REVISION_IMAGES = 4  # capped combined vision payload -- a dozen full-page renders was observed to blow
                          # past REQUEST_TIMEOUT_SEC on modest local hardware before even finishing prompt
                          # processing (vision models often need >=1000+ tokens per image just to encode
                          # it), so start conservative -- llm_client._chat_multimodal() halves further and
                          # retries on its own if even this many still times out or overflows context


class EstimateRevisionWorker(QThread):
    done = pyqtSignal(int)
    failed = pyqtSignal(str)

    def __init__(self, pdf_path, covered_slides, llm_client=None):
        super().__init__()
        self.pdf_path = pdf_path
        self.covered_slides = covered_slides
        self.llm = llm_client or LLMClient()

    def run(self):
        try:
            text, images = syllabus_parser.extract_text_and_images(
                self.pdf_path, max_images=MAX_REVISION_IMAGES, page_limit=self.covered_slides,
            )
            minutes = self.llm.estimate_revision_minutes(text, self.covered_slides, images_b64=images)
            self.done.emit(minutes)
        except REVISION_ERRORS as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"Unexpected error: {e}")


class RevisionPanel(QWidget):
    # (pdf_path, total_minutes, covered_slides) -- whatever's listening should
    # actually start a monitored session with these; this panel never starts
    # one itself. covered_slides feeds the lockdown pacing progress bar (see
    # LockdownWindow) -- it's the exact slide count the LLM's estimate was
    # actually based on, more accurate than the PDF's raw page count would be.
    session_requested = pyqtSignal(str, int, int)

    def __init__(self, llm_client=None, parent=None):
        super().__init__(parent)
        self.llm = llm_client or LLMClient()
        self.pdf_path = None
        self.total_slides = 0
        self._estimate_worker = None
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        outer.addWidget(card)

        self.upload_button = QPushButton("Upload slides (PDF)...")
        self.upload_button.clicked.connect(self._on_upload_clicked)
        layout.addWidget(self.upload_button)

        self.file_label = QLabel("No slides uploaded")
        self.file_label.setWordWrap(True)
        layout.addWidget(self.file_label)

        form = QFormLayout()

        self.covered_spin = QSpinBox()
        self.covered_spin.setRange(1, 1)
        self.covered_spin.setEnabled(False)
        form.addRow("Covered up to slide:", self.covered_spin)

        layout.addLayout(form)

        self.estimate_button = QPushButton("Estimate revision time")
        self.estimate_button.setEnabled(False)
        self.estimate_button.clicked.connect(self._on_estimate_clicked)
        layout.addWidget(self.estimate_button)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        minutes_form = QFormLayout()
        self.minutes_spin = QSpinBox()
        self.minutes_spin.setRange(10, 600)
        self.minutes_spin.setValue(50)
        self.minutes_spin.setSuffix(" min")
        minutes_form.addRow("Revision time needed:", self.minutes_spin)
        layout.addLayout(minutes_form)

        self.start_button = QPushButton("Start Revision Session")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self._on_start_clicked)
        layout.addWidget(self.start_button)

        layout.addStretch()

    def _set_busy(self, busy):
        self.upload_button.setEnabled(not busy)
        self.covered_spin.setEnabled(not busy and self.total_slides > 0)
        self.estimate_button.setEnabled(not busy and self.pdf_path is not None)
        self.minutes_spin.setEnabled(not busy)
        self.start_button.setEnabled(not busy and self.pdf_path is not None)

    # ---- uploading slides ----

    def _on_upload_clicked(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select slides", "", "PDF files (*.pdf)")
        if not path:
            return
        try:
            self.total_slides = syllabus_parser.count_pdf_pages(path)
        except SyllabusParseError as e:
            QMessageBox.warning(self, "Couldn't read slides", str(e))
            return

        self.pdf_path = path
        self.file_label.setText(f"{Path(path).name} -- {self.total_slides} slide(s)")
        self.covered_spin.setRange(1, self.total_slides)
        self.covered_spin.setValue(self.total_slides)
        self.status_label.setText("")
        self._set_busy(False)

    # ---- estimating revision time ----

    def _on_estimate_clicked(self):
        if self.pdf_path is None:
            return
        self.status_label.setText("Estimating revision time needed...")
        self._set_busy(True)
        self._estimate_worker = EstimateRevisionWorker(self.pdf_path, self.covered_spin.value(), llm_client=self.llm)
        self._estimate_worker.done.connect(self._on_estimated)
        self._estimate_worker.failed.connect(self._on_estimate_failed)
        self._estimate_worker.start()

    def _on_estimated(self, minutes):
        self._set_busy(False)
        self.minutes_spin.setValue(minutes)
        self.status_label.setText(
            f"Estimated {minutes} minute(s) to fully revise slides 1-{self.covered_spin.value()} -- "
            "adjust above if you'd like, then start."
        )

    def _on_estimate_failed(self, message):
        self._set_busy(False)
        self.status_label.setText(f"Couldn't estimate: {message}")

    # ---- starting the session ----

    def _on_start_clicked(self):
        if self.pdf_path is None:
            return
        self.session_requested.emit(self.pdf_path, self.minutes_spin.value(), self.covered_spin.value())
