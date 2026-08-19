# A literal fill-in-the-blanks form for the exam table -- not a chat.
# Workflow: pick a column (an existing course, or an empty one for a new
# course), optionally paste a screenshot (e.g. a syllabus grading table)
# and click "Extract from image" to pre-fill whatever it can read, then
# review/edit any field by hand, then Submit.
#
# The AI's role is split cleanly in two, on purpose:
#   - Extraction (paste screenshot -> pre-filled fields) is genuinely
#     fuzzy -- the model is looking at an image and guessing -- so its
#     output is only ever a SUGGESTION sitting in an editable text box,
#     never written to Notion directly.
#   - Submit sends exactly what's currently in the form's fields -- text
#     you've already reviewed, with zero ambiguity left -- through
#     NotionAgent.chat() so the same tool-calling machinery (and its
#     success/failure surfacing) does the actual write. By the time it
#     reaches the model here, there's nothing left for it to interpret or
#     get wrong; it's just executing values you already approved.
#
# HOW TO EMBED: plain QWidget.
#     from exam_form_panel import ExamFormPanel
#     panel = ExamFormPanel(notion_token="...", table_block_id="...")

import base64
import os
import re
from datetime import datetime

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QMessageBox, QScrollArea
)
from PyQt6.QtCore import QBuffer, QIODevice, QSettings, QThread, pyqtSignal, Qt
from PyQt6.QtGui import QImage

from notion_agent import NotionAgent, NotionAgentError
from notion_table_client import NotionTableError
from notion_page_client import format_week_date_range
from notion_client import NotionClientError
from llm_client import LLMClientError
from theme import Card

AGENT_ERRORS = (NotionAgentError, LLMClientError, NotionTableError, NotionClientError)

# Shown as this page's header help icon (see theme.CollapsibleSection's
# help_text param, wired in ui/main_window.py) rather than as a permanent
# inline label -- "how does this page work" text.
EXAM_FORM_HELP_TEXT = (
    "Pick a column (an existing course, or an empty one for a new course). Paste a screenshot "
    "and click \"Extract from image\" to pre-fill what it can read -- review and correct "
    "anything before you Submit. Submit writes exactly what's in these fields, nothing more."
)

SETTINGS_ORG = "StudyWarden"
SETTINGS_APP = "StudyWarden"
SETTINGS_KEY_WEEK_START = "week1_start_date"  # same key ui/settings_dialog.py's Week Settings group writes
DEFAULT_WEEK1_START = datetime(2026, 8, 9).date()  # matches settings_dialog.py's DEFAULT_WEEK1_START

# Matches a value that's EXACTLY a bare week reference (nothing else in the
# string) -- see extract_form_fields()'s prompt, which asks the model to
# output precisely this format when it has no explicit date to fall back
# on. Anything messier (a week number mixed with other text) is left
# untouched rather than guessed at.
_WEEK_ONLY_PATTERN = re.compile(r"^\s*week\s*(\d+)\s*$", re.IGNORECASE)

# The form's fields are NOT a fixed list -- they're rebuilt from whatever
# the real table's rows actually are every time it loads (see
# _rebuild_form_fields()), since the real table's rows have already changed
# more than once. "course_name" is the exact string NotionAgent's
# update_exam_cell tool expects for that special row; every other field name
# is whatever TableState.field_names() returns for the table as currently
# read from Notion.

# Domain-specific guidance for extract_form_fields() -- real syllabi use
# wildly different terminology and layouts (tables, bullet lists, merged
# cells, batched items) for the same underlying fields; this maps that
# variety onto this app's fixed schema. See AI/llm_client.py's base prompt
# for the generic (date format / week-number / TBA) rules this adds to.
_EXTRACTION_CONTEXT = """This is a course syllabus grading/schedule table or list -- it may be a plain \
table, a bulleted list, or a table with merged/multi-line cells, and may use different words than the \
field names below for the same thing. Map whatever terminology it actually uses onto these exact fields, \
using your best judgment for synonyms:

- "Quiz-1"/"Quiz-2"/"Quiz-3"/"Quiz-4"/"Quiz-5": quizzes, in the order they appear ("Quiz 1", "Quiz-I", \
"First Quiz" -> Quiz-1; the next one -> Quiz-2; and so on). If one row lists a batch like "2 Quizzes" or \
"2x Quizzes" with two separate dates for it, split them across Quiz-1 and Quiz-2 -- do not combine both \
dates into a single field.
- "Major-1"/"Major-2": the midterm exam(s) -- "Midterm Exam", "Major Exam I/II", "Midterm I/II" all mean \
this. Major-1 is the first (or only) one, Major-2 is a second one if the course has one.
- "Final": the final exam.
- "Project"/"Project-2": course project(s) -- "Guided Course Project", "Project Milestone 1/2", etc. If \
there are two distinct projects or milestones, Project is the first, Project-2 is the second.
- "Assignments"/"Assignments-2": graded assignments/homework/programming assignments. If there are more \
than two distinct batches of these (e.g. separate "Programming Assignments" AND "Homework Assignments" \
groups), use your judgment about which two are most central to the course and fit them into these two \
slots -- there's no field for a third batch.
- "Certificate or Coursera": an external certificate/Coursera/online-course completion requirement, if \
any is mentioned.

A course-level "Attendance" percentage/line item has no field of its own here -- ignore it entirely, \
it is not one of the requested fields and should not be put anywhere."""


def _week1_start_date():
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    raw = settings.value(SETTINGS_KEY_WEEK_START)
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            pass
    return DEFAULT_WEEK1_START


def _convert_week_references(fields):
    """Replaces any field value that's exactly a bare "Week N" reference
    with a real date range (e.g. "Week 3" -> "August 23rd-29th"), computed
    from the "week 1 starts on" date in Settings -- the same date the
    to-do list's auto-syncing week number already uses. A value that isn't
    a clean week-only reference (a real date, "TBA", "N/A", or a week
    number mixed with other text) passes through unchanged."""
    week1_start = _week1_start_date()
    converted = {}
    for field, value in fields.items():
        match = _WEEK_ONLY_PATTERN.match(value)
        converted[field] = format_week_date_range(int(match.group(1)), week1_start) if match else value
    return converted


def _qimage_to_png_b64(image):
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.ReadWrite)
    image.save(buffer, "PNG")
    return base64.b64encode(bytes(buffer.data())).decode("ascii")


def _build_submit_instruction(column, values):
    lines = [
        f"Write these exact values to column {column} of the exam table -- call update_exam_cell "
        "once for each one listed below, using exactly the value given (an empty value means clear "
        "that field). Do not skip any of them, do not ask for clarification, do not describe what "
        "you're about to do first -- just call the tools, right now, for every field listed."
    ]
    for field, value in values.items():
        lines.append(f"- field={field!r}, value={value!r}")
    return "\n".join(lines)


class LoadTableWorker(QThread):
    done = pyqtSignal(object)  # TableState
    failed = pyqtSignal(str)

    def __init__(self, agent):
        super().__init__()
        self.agent = agent

    def run(self):
        try:
            self.done.emit(self.agent.refresh())
        except AGENT_ERRORS as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"Unexpected error: {e}")


class ExtractFieldsWorker(QThread):
    done = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, agent, field_names, images_b64):
        super().__init__()
        self.agent = agent
        self.field_names = field_names
        self.images_b64 = images_b64

    def run(self):
        try:
            fields = self.agent.llm.extract_form_fields(
                self.field_names, self.images_b64, context=_EXTRACTION_CONTEXT,
            )
            self.done.emit(fields)
        except AGENT_ERRORS as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"Unexpected error: {e}")


class SubmitWorker(QThread):
    done = pyqtSignal(str)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)
    tool_result = pyqtSignal(str, dict, dict)

    def __init__(self, agent, instruction):
        super().__init__()
        self.agent = agent
        self.instruction = instruction

    def run(self):
        try:
            reply, _ = self.agent.chat(
                self.instruction, on_progress=self.progress.emit, on_tool_result=self.tool_result.emit,
            )
            self.done.emit(reply)
        except AGENT_ERRORS as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"Unexpected error: {e}")


class ExamFormPanel(QWidget):
    def __init__(self, notion_token, table_block_id, calendar_database_id=None, parent=None, todo_page=None):
        super().__init__(parent)
        self.agent = NotionAgent(
            notion_token, table_block_id, calendar_database_id=calendar_database_id, todo_page=todo_page,
        )
        self.table_state = None
        self.attached_images = []
        self._load_worker = None
        self._extract_worker = None
        self._submit_worker = None
        self._build_ui()
        self._reload_table()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(14)

        setup_card = Card()
        setup_layout = QVBoxLayout(setup_card)
        setup_layout.setContentsMargins(18, 18, 18, 18)
        outer.addWidget(setup_card)

        self.column_combo = QComboBox()
        self.column_combo.currentIndexChanged.connect(self._on_column_changed)
        setup_layout.addWidget(self.column_combo)

        self.reload_button = QPushButton("Reload from Notion")
        self.reload_button.clicked.connect(self._reload_table)
        setup_layout.addWidget(self.reload_button)

        screenshot_row = QHBoxLayout()
        self.paste_button = QPushButton("Paste screenshot")
        self.paste_button.clicked.connect(self._on_paste_clicked)
        screenshot_row.addWidget(self.paste_button)
        self.extract_button = QPushButton("Extract from image")
        self.extract_button.setEnabled(False)
        self.extract_button.clicked.connect(self._on_extract_clicked)
        screenshot_row.addWidget(self.extract_button)
        self.clear_images_button = QPushButton("Clear attached files")
        self.clear_images_button.setEnabled(False)
        self.clear_images_button.clicked.connect(self._on_clear_images_clicked)
        screenshot_row.addWidget(self.clear_images_button)
        setup_layout.addLayout(screenshot_row)

        self.attached_label = QLabel("No screenshots attached")
        self.attached_label.setWordWrap(True)
        setup_layout.addWidget(self.attached_label)

        self.form_container = Card()
        self.form_layout = QFormLayout(self.form_container)
        self.form_layout.setContentsMargins(18, 18, 18, 18)
        self.field_edits = {}  # populated by _rebuild_form_fields() once the real table loads

        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setWidget(self.form_container)
        outer.addWidget(form_scroll, 1)

        self.fill_na_button = QPushButton("Fill empty fields with N/A")
        self.fill_na_button.clicked.connect(self._on_fill_na_clicked)
        outer.addWidget(self.fill_na_button)

        self.submit_button = QPushButton("Submit")
        self.submit_button.setEnabled(False)
        self.submit_button.clicked.connect(self._on_submit_clicked)
        outer.addWidget(self.submit_button)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

    # ---- busy state ----

    def _set_busy(self, busy):
        self.column_combo.setEnabled(not busy)
        self.reload_button.setEnabled(not busy)
        self.paste_button.setEnabled(not busy)
        self.clear_images_button.setEnabled(not busy and bool(self.attached_images))
        self.extract_button.setEnabled(not busy and bool(self.attached_images))
        self.fill_na_button.setEnabled(not busy)
        self.submit_button.setEnabled(not busy and self.table_state is not None)
        for edit in self.field_edits.values():
            edit.setEnabled(not busy)

    # ---- quick N/A fill ----

    def _on_fill_na_clicked(self):
        # Never touches course_name -- an empty course name should be
        # picked/typed deliberately, not silently defaulted to "N/A".
        for field, edit in self.field_edits.items():
            if field != "course_name" and not edit.text().strip():
                edit.setText("N/A")

    # ---- form fields ----

    def _rebuild_form_fields(self, field_names):
        """(Re)builds the form's text boxes from the real table's current
        fields -- not a fixed list, since the real table's rows have
        already changed more than once as the user edits their real Notion
        table. field_names: ["course_name", <every other real row label, in
        real table order>]."""
        while self.form_layout.rowCount():
            self.form_layout.removeRow(0)
        self.field_edits = {}
        for field in field_names:
            edit = QLineEdit()
            label = "Course name" if field == "course_name" else field
            self.form_layout.addRow(f"{label}:", edit)
            self.field_edits[field] = edit

    # ---- loading the table ----

    def _reload_table(self):
        self.status_label.setText("Loading exam table...")
        self._set_busy(True)
        self._load_worker = LoadTableWorker(self.agent)
        self._load_worker.done.connect(self._on_table_loaded)
        self._load_worker.failed.connect(self._on_failed)
        self._load_worker.start()

    def _on_table_loaded(self, state):
        self.table_state = state
        self._rebuild_form_fields(["course_name"] + state.field_names())
        self._set_busy(False)
        self.status_label.setText("")

        current_column = self.column_combo.currentIndex()
        self.column_combo.blockSignals(True)
        self.column_combo.clear()
        for col in range(state.num_columns):
            name = state.course_name(col)
            self.column_combo.addItem(f"Column {col} -- {name if name else '(empty)'}")
        self.column_combo.blockSignals(False)

        if state.num_columns:
            restore_index = current_column if 0 <= current_column < state.num_columns else 0
            self.column_combo.setCurrentIndex(restore_index)
            self._load_column_into_fields(restore_index)
        self.submit_button.setEnabled(state.num_columns > 0)

    def _on_failed(self, message):
        self._set_busy(False)
        self.status_label.setText(f"Something went wrong: {message}")

    # ---- column selection ----

    def _on_column_changed(self, index):
        if index < 0 or self.table_state is None:
            return
        self._load_column_into_fields(index)

    def _load_column_into_fields(self, column):
        state = self.table_state
        self.field_edits["course_name"].setText(state.course_name(column))
        for field in state.field_names():
            row = state.row_index_for_field(field)
            self.field_edits[field].setText(state.grid[row][column])

    # ---- screenshots ----

    def _on_paste_clicked(self):
        from PyQt6.QtWidgets import QApplication
        image = QApplication.clipboard().image()
        if image.isNull():
            QMessageBox.information(self, "Nothing to paste", "No image found in the clipboard.")
            return
        self.attached_images.append(image)
        self._update_attached_label()

    def _on_clear_images_clicked(self):
        self.attached_images = []
        self._update_attached_label()

    def _update_attached_label(self):
        count = len(self.attached_images)
        if count == 0:
            self.attached_label.setText("No screenshots attached")
        elif count == 1:
            self.attached_label.setText("1 screenshot attached")
        else:
            self.attached_label.setText(f"{count} screenshots attached")
        self.clear_images_button.setEnabled(count > 0)
        self.extract_button.setEnabled(count > 0)

    # ---- extraction ----

    def _on_extract_clicked(self):
        if not self.attached_images:
            return
        self.status_label.setText("Reading the image(s)...")
        self._set_busy(True)
        images_b64 = [_qimage_to_png_b64(image) for image in self.attached_images]
        field_names = list(self.field_edits.keys())
        self._extract_worker = ExtractFieldsWorker(self.agent, field_names, images_b64)
        self._extract_worker.done.connect(self._on_extracted)
        self._extract_worker.failed.connect(self._on_failed)
        self._extract_worker.start()

    def _on_extracted(self, fields):
        self._set_busy(False)
        # Bare "Week N" references (the model's own output when no explicit
        # date was available -- see extract_form_fields()'s prompt) become
        # real date ranges here, using the "week 1 starts on" date from
        # Settings -- deterministic, not something asked of the model.
        fields = _convert_week_references(fields)
        # Only overwrite a field if the extraction actually found something
        # for it -- an empty result means "not visible in the image", not
        # "clear whatever's already in the box".
        filled = 0
        for field, value in fields.items():
            if value:
                self.field_edits[field].setText(value)
                filled += 1

        extracted_course = fields.get("course_name", "").strip()
        if extracted_course and self.table_state is not None:
            match = next(
                (col for col in range(self.table_state.num_columns)
                 if self.table_state.course_name(col).strip().lower() == extracted_course.lower()),
                None,
            )
            if match is None:
                match = next(
                    (col for col in range(self.table_state.num_columns)
                     if not self.table_state.course_name(col).strip()),
                    None,
                )
            if match is not None and match != self.column_combo.currentIndex():
                self.column_combo.setCurrentIndex(match)
                self._load_column_into_fields(match)
                for field, value in fields.items():
                    if value:
                        self.field_edits[field].setText(value)

        self.status_label.setText(f"Filled in {filled} field(s) from the image -- review before submitting.")

    # ---- submit ----

    def _on_submit_clicked(self):
        if self.table_state is None or self.column_combo.currentIndex() < 0:
            return
        column = self.column_combo.currentIndex()
        values = {field: edit.text().strip() for field, edit in self.field_edits.items()}
        instruction = _build_submit_instruction(column, values)

        self.status_label.setText("Submitting...")
        self._set_busy(True)
        self._submit_worker = SubmitWorker(self.agent, instruction)
        self._submit_worker.done.connect(self._on_submitted)
        self._submit_worker.failed.connect(self._on_failed)
        self._submit_worker.progress.connect(self._on_submit_progress)
        self._submit_worker.tool_result.connect(self._on_submit_tool_result)
        self._submit_worker.start()

    def _on_submit_progress(self, phase_text):
        self.status_label.setText(phase_text)

    def _on_submit_tool_result(self, name, arguments, result):
        if result.get("error"):
            QMessageBox.warning(self, "A field failed to save", f"{name} failed: {result['error']}")

    def _on_submitted(self, reply):
        self._set_busy(False)
        self.status_label.setText("Submitted.")
        self._reload_table()


if __name__ == "__main__":
    # Standalone test run: `python AI/exam_form_panel.py`
    notion_token = os.environ.get("NOTION_TOKEN")
    table_block_id = os.environ.get("NOTION_TABLE_BLOCK_ID")
    if not notion_token or not table_block_id:
        print(
            "Set NOTION_TOKEN and NOTION_TABLE_BLOCK_ID environment variables first.\n"
            "PowerShell example:\n"
            '  $env:NOTION_TOKEN = "secret_..."\n'
            '  $env:NOTION_TABLE_BLOCK_ID = "..."\n'
            "  py AI/exam_form_panel.py"
        )
        raise SystemExit(1)

    import sys
    from PyQt6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    window = ExamFormPanel(notion_token=notion_token, table_block_id=table_block_id)
    window.resize(600, 700)
    window.show()
    sys.exit(app.exec())
