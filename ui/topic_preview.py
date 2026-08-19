# "Preview State": a mandatory screen shown right before Monitor Lockdown
# activates -- shows 3-5 thematic topics (+ a short synopsis each) from
# today's study material, so you get a few seconds of "mental scaffolding"
# (a quick reminder of what you're about to study) instead of diving in
# cold.
#
# Normally the topics were already computed when the plan was generated in
# the Study Planner (see StudyPlanner.generate_plan()'s "topics" field) --
# pass those in via `topics` and this shows them instantly, no waiting.
# That's the important part: this used to run the (often slow, especially
# for image-heavy material on a vision model) LLM call live, right here,
# with the focus timer already ticking -- which could run the timer out
# before the preview even finished. If no precomputed topics are available
# (an older plan from before this existed, or extraction failed at
# generation time), it falls back to live extraction via `llm_client` +
# `pdf_path`.
#
# "Mandatory" here means the Continue button (and Escape/closing the
# dialog) stay disabled until resolved -- for the live-extraction fallback,
# that means until the attempt resolves; it does NOT mean you're stuck
# forever if the LLM call fails (e.g. LM Studio isn't running): failure
# still unlocks Continue, so this never becomes a hard blocker to actually
# starting your focus session.

import syllabus_parser
from llm_client import LLMClientError
from syllabus_parser import SyllabusParseError

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QHeaderView
)

PREVIEW_ERRORS = (LLMClientError, SyllabusParseError)


class TopicExtractionWorker(QThread):
    done = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, llm_client, pdf_path):
        super().__init__()
        self.llm_client = llm_client
        self.pdf_path = pdf_path

    def run(self):
        try:
            text, images = syllabus_parser.extract_text_and_images(self.pdf_path)
            topics = self.llm_client.extract_topics(text, images_b64=images)
            self.done.emit(topics)
        except PREVIEW_ERRORS as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"Unexpected error: {e}")


class TopicPreviewDialog(QDialog):
    """Modal by design -- see module docstring for what "mandatory" means
    here. Construct and call exec(); it blocks the caller until you're
    allowed past it. Pass precomputed `topics` for the fast (usual) path,
    or `llm_client` + `pdf_path` for the live-extraction fallback."""

    def __init__(self, topics=None, llm_client=None, pdf_path=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Today's Topics")
        self.setModal(True)
        self.resize(700, 480)

        layout = QVBoxLayout(self)

        info = QLabel(
            "A quick preview of what today's material covers before you lock in -- "
            "worth a few seconds to prime your memory."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Topic", "Synopsis"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)

        self.continue_button = QPushButton("Continue to Lockdown")
        self.continue_button.setEnabled(False)
        self.continue_button.clicked.connect(self.accept)
        layout.addWidget(self.continue_button)

        self._worker = None
        if topics:
            self._on_topics_ready(topics)
        elif llm_client is not None and pdf_path is not None:
            self.status_label.setText("Generating preview...")
            self._worker = TopicExtractionWorker(llm_client, pdf_path)
            self._worker.done.connect(self._on_topics_ready)
            self._worker.failed.connect(self._on_topics_failed)
            self._worker.start()
        else:
            self._on_topics_failed("No material to summarize.")

    def _on_topics_ready(self, topics):
        self.status_label.setText(f"{len(topics)} topic(s) found in today's material:")
        self.table.setRowCount(0)
        for topic in topics:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(topic["topic"]))
            self.table.setItem(row, 1, QTableWidgetItem(topic["synopsis"]))
        self.table.resizeRowsToContents()
        self.continue_button.setEnabled(True)

    def _on_topics_failed(self, message):
        self.status_label.setText(
            f"Couldn't generate a topic preview: {message}\n\n"
            "You can still continue -- this just won't have a topic summary today."
        )
        self.continue_button.setEnabled(True)

    # ---- block skipping past the mandatory wait, but never trap you once it resolves ----

    def closeEvent(self, event):
        if self.continue_button.isEnabled():
            event.accept()
        else:
            event.ignore()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and not self.continue_button.isEnabled():
            return
        super().keyPressEvent(event)
