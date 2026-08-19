# UI for the study planner: pick a course + exam field from the schedule
# table, say how many chapters it covers, optionally attach study
# material, and generate a day-by-day study calendar. Nothing gets written
# to Notion until you review the generated plan and click "Add to
# Calendar" -- generate and commit are two separate, explicit steps.
#
# Per-exam settings (chapter count, material, daily minutes) are remembered
# (QSettings) so you only enter them once. If the exam's date on the
# schedule table changes since the last plan was generated for it, picking
# that exam automatically regenerates the preview using the remembered
# settings -- you still get a chance to review before it writes anything,
# but you don't have to re-type the chapter count/material every time.
#
# HOW TO EMBED: plain QWidget.
#     from study_planner_panel import StudyPlannerPanel
#     panel = StudyPlannerPanel(notion_token="...", table_block_id="...", calendar_database_id="...")

import json
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QComboBox, QSpinBox,
    QPushButton, QFileDialog, QMessageBox, QTableWidget, QTableWidgetItem, QHeaderView, QLineEdit
)
from PyQt6.QtCore import QThread, pyqtSignal, QSettings

from study_planner import StudyPlanner, StudyPlannerError, WORK_FIELD_LABELS
from llm_client import LLMClientError
from syllabus_parser import SyllabusParseError
from notion_table_client import NotionTableError
from notion_client import NotionClientError
from theme import Card

PLANNER_ERRORS = (StudyPlannerError, LLMClientError, SyllabusParseError, NotionTableError, NotionClientError)

# Shown as this page's header help icon (see theme.CollapsibleSection's
# help_text param, wired in ui/main_window.py) rather than as a permanent
# inline label -- "how does this page work" text.
STUDY_PLANNER_HELP_TEXT = (
    "Pick a deadline from the schedule table -- an exam (tell it how many chapters it "
    "covers) or an assignment/Project (describe what it needs, and/or attach the "
    "instructions/rubric) -- and optionally attach material. It estimates the total time "
    "needed and spreads the sessions evenly across the days leading up to the deadline "
    "(not crammed at the end), at most the given minutes per day. Each day can hold one "
    "exam-study session AND one assignment/project session, but not two of the same kind -- "
    "it works around whatever else is already on your calendar or due around the same time. "
    "Nothing is added to the calendar until you review the plan and click \"Add to Calendar\"."
)

SETTINGS_ORG = "StudyWarden"
SETTINGS_APP = "StudyWarden"


def _config_key(course, field):
    return f"study_plan/{course}|{field}"


def _stored_material_paths(stored):
    # older saved configs used a single "material_path" string
    if "material_paths" in stored:
        return stored.get("material_paths") or []
    legacy = stored.get("material_path")
    return [legacy] if legacy else []


def find_todays_material(planner, today=None):
    """Looks up what's scheduled for today (StudyPlanner.todays_sessions())
    and returns (pdf_path, topics, session) from whatever was last saved
    for that course/field's plan: pdf_path is the first attached PDF
    remembered (or None), topics is the topic list computed WHEN THE PLAN
    WAS GENERATED (see StudyPlanner.generate_plan()'s "topics" field) --
    i.e. already done ahead of time, not something Monitor Lockdown has to
    wait on. topics is [] if none was ever computed (an older plan from
    before this existed, or extraction failed at generation time -- the
    topic preview falls back to live extraction in that case). session is
    the matched {"course", "field", "date_text", "date"} dict (see
    StudyPlanner.todays_sessions()) -- e.g. for a mid-session snapshot to
    record WHICH scheduled item this was (see core/session_snapshot.py).
    Returns (None, [], None) if there's no session today or nothing was
    ever saved for it."""
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    for session in planner.todays_sessions(today=today):
        raw = settings.value(_config_key(session["course"], session["field"]))
        if not raw:
            continue
        try:
            stored = json.loads(raw)
        except (TypeError, ValueError):
            continue
        pdf_path = next(
            (p for p in _stored_material_paths(stored) if p and p.lower().endswith(".pdf")), None
        )
        topics = stored.get("topics") or []
        if pdf_path or topics:
            return pdf_path, topics, session
    return None, [], None


class ListExamsWorker(QThread):
    done = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, planner):
        super().__init__()
        self.planner = planner

    def run(self):
        try:
            self.done.emit(self.planner.list_exams())
        except PLANNER_ERRORS as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"Unexpected error: {e}")


class GeneratePlanWorker(QThread):
    done = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, planner, course, field, chapter_count, description, material_paths, daily_minutes):
        super().__init__()
        self.planner = planner
        self.course = course
        self.field = field
        self.chapter_count = chapter_count
        self.description = description
        self.material_paths = material_paths
        self.daily_minutes = daily_minutes

    def run(self):
        try:
            plan = self.planner.generate_plan(
                self.course, self.field, chapter_count=self.chapter_count, description=self.description,
                material_paths=self.material_paths, daily_minutes=self.daily_minutes,
            )
            self.done.emit(plan)
        except PLANNER_ERRORS as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"Unexpected error: {e}")


class CommitPlanWorker(QThread):
    done = pyqtSignal(int)
    failed = pyqtSignal(str)

    def __init__(self, planner, sessions):
        super().__init__()
        self.planner = planner
        self.sessions = sessions

    def run(self):
        try:
            self.planner.commit_plan(self.sessions)
            self.done.emit(len(self.sessions))
        except PLANNER_ERRORS as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"Unexpected error: {e}")


class StudyPlannerPanel(QWidget):
    def __init__(self, notion_token, table_block_id, calendar_database_id, parent=None):
        super().__init__(parent)
        self.planner = StudyPlanner(notion_token, table_block_id, calendar_database_id)
        self.exams = []          # [{"course","field","date_text","date"}, ...]
        self.current_plan = None  # last generate_plan() result
        self.material_paths = []  # list of attached file paths, combined into one document
        self._worker = None
        self._settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self._build_ui()
        self._reload_exams()

    # ---- remembering per-exam settings ----

    def _load_stored_config(self, course, field):
        raw = self._settings.value(_config_key(course, field))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def _save_stored_config(self, plan):
        data = {
            "chapter_count": self.chapter_spin.value(),
            "description": self.description_edit.text(),
            "material_paths": self.material_paths,
            "daily_minutes": self.daily_minutes_spin.value(),
            "last_exam_date": plan["exam_date"].isoformat(),
            "topics": plan.get("topics", []),
        }
        self._settings.setValue(_config_key(plan["course"], plan["field"]), json.dumps(data))

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)

        setup_card = Card()
        layout = QVBoxLayout(setup_card)
        layout.setContentsMargins(18, 18, 18, 18)
        outer.addWidget(setup_card)

        form = QFormLayout()

        self.course_combo = QComboBox()
        self.course_combo.currentIndexChanged.connect(self._on_course_changed)
        form.addRow("Course:", self.course_combo)

        self.field_combo = QComboBox()
        self.field_combo.currentIndexChanged.connect(self._on_field_changed)
        form.addRow("Deadline:", self.field_combo)

        self.chapter_spin = QSpinBox()
        self.chapter_spin.setRange(1, 100)
        self.chapter_spin.setValue(1)
        self._chapter_row_label = QLabel("Chapters covered:")
        form.addRow(self._chapter_row_label, self.chapter_spin)

        self.description_edit = QLineEdit()
        self.description_edit.setPlaceholderText("e.g. build a small REST API with tests and a writeup")
        self._description_row_label = QLabel("Details:")
        form.addRow(self._description_row_label, self.description_edit)

        self.daily_minutes_spin = QSpinBox()
        self.daily_minutes_spin.setRange(10, 600)
        self.daily_minutes_spin.setValue(100)
        self.daily_minutes_spin.setSuffix(" min/day")
        form.addRow("Daily time budget:", self.daily_minutes_spin)

        layout.addLayout(form)
        self._form = form

        self.attach_button = QPushButton("Attach files...")
        self.attach_button.clicked.connect(self._on_attach_clicked)
        layout.addWidget(self.attach_button)
        self.material_label = QLabel("No files selected")
        self.material_label.setWordWrap(True)
        layout.addWidget(self.material_label)
        self.clear_material_button = QPushButton("Clear attached files")
        self.clear_material_button.setEnabled(False)
        self.clear_material_button.clicked.connect(self._on_clear_material_clicked)
        layout.addWidget(self.clear_material_button)

        self.generate_button = QPushButton("Generate Plan")
        self.generate_button.clicked.connect(self._on_generate_clicked)
        layout.addWidget(self.generate_button)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        results_card = Card()
        results_layout = QVBoxLayout(results_card)
        results_layout.setContentsMargins(18, 18, 18, 18)
        outer.addWidget(results_card)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Date", "Session"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        results_layout.addWidget(self.table)

        self.commit_button = QPushButton("Add to Calendar")
        self.commit_button.setEnabled(False)
        self.commit_button.clicked.connect(self._on_commit_clicked)
        results_layout.addWidget(self.commit_button)

    # ---- loading the exam picker ----

    def _reload_exams(self):
        self.status_label.setText("Checking schedule table...")
        self._set_busy(True)
        self._worker = ListExamsWorker(self.planner)
        self._worker.done.connect(self._on_exams_loaded)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_exams_loaded(self, exams):
        self._set_busy(False)
        self.exams = exams
        self.course_combo.clear()
        seen = []
        for e in exams:
            if e["course"] not in seen:
                seen.append(e["course"])
        self.course_combo.addItems(seen)
        if not seen:
            self.status_label.setText(
                "No courses with a real exam date found yet -- fill in some dates on the "
                "schedule table first (the Schedule Assistant tab can do this)."
            )
        else:
            self.status_label.setText("")
        self._on_course_changed()

    def _on_course_changed(self):
        course = self.course_combo.currentText()
        self.field_combo.clear()
        self.field_combo.addItems([e["field"] for e in self.exams if e["course"] == course])

    def _on_field_changed(self):
        course = self.course_combo.currentText()
        field = self.field_combo.currentText()
        if not course or not field:
            return

        is_work_type = field in WORK_FIELD_LABELS
        self._form.setRowVisible(self.chapter_spin, not is_work_type)
        self._form.setRowVisible(self.description_edit, is_work_type)

        stored = self._load_stored_config(course, field)
        self.chapter_spin.setValue(stored.get("chapter_count", 1) if stored else 1)
        self.description_edit.setText(stored.get("description", "") if stored else "")
        self.daily_minutes_spin.setValue(stored.get("daily_minutes", 100) if stored else 100)
        self.material_paths = _stored_material_paths(stored) if stored else []
        self._update_material_label()

        if not stored:
            return

        exam = next((e for e in self.exams if e["course"] == course and e["field"] == field), None)
        if exam is None:
            return
        current_date_iso = exam["date"].isoformat()
        stored_date_iso = stored.get("last_exam_date")
        if stored_date_iso and stored_date_iso != current_date_iso:
            self.status_label.setText(
                f"Exam date changed since your last plan ({stored_date_iso} -> {current_date_iso}) -- "
                "regenerating with your remembered settings..."
            )
            self._on_generate_clicked()

    def _on_failed(self, message):
        self._set_busy(False)
        self.status_label.setText(f"Something went wrong: {message}")

    def _set_busy(self, busy):
        self.course_combo.setEnabled(not busy)
        self.field_combo.setEnabled(not busy)
        self.chapter_spin.setEnabled(not busy)
        self.description_edit.setEnabled(not busy)
        self.daily_minutes_spin.setEnabled(not busy)
        self.attach_button.setEnabled(not busy)
        self.clear_material_button.setEnabled(not busy and bool(self.material_paths))
        self.generate_button.setEnabled(not busy)
        if busy:
            self.commit_button.setEnabled(False)

    # ---- attaching material ----

    def _on_attach_clicked(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select study material", "", "Documents (*.pdf *.docx *.txt *.jpg *.jpeg *.png)"
        )
        if not paths:
            return
        self.material_paths = paths
        self._update_material_label()

    def _on_clear_material_clicked(self):
        self.material_paths = []
        self._update_material_label()

    def _update_material_label(self):
        if not self.material_paths:
            self.material_label.setText("No files selected")
        elif len(self.material_paths) == 1:
            self.material_label.setText(Path(self.material_paths[0]).name)
        else:
            names = ", ".join(Path(p).name for p in self.material_paths)
            self.material_label.setText(f"{len(self.material_paths)} files: {names}")
        self.clear_material_button.setEnabled(bool(self.material_paths))

    # ---- generating the plan ----

    def _on_generate_clicked(self):
        course = self.course_combo.currentText()
        field = self.field_combo.currentText()
        if not course or not field:
            QMessageBox.information(self, "Nothing to plan", "No exam is selected.")
            return

        is_work_type = field in WORK_FIELD_LABELS
        self.status_label.setText("Estimating time needed and summarizing key topics...")
        self.table.setRowCount(0)
        self.commit_button.setEnabled(False)
        self._set_busy(True)
        self._worker = GeneratePlanWorker(
            self.planner, course, field,
            chapter_count=None if is_work_type else self.chapter_spin.value(),
            description=self.description_edit.text() if is_work_type else None,
            material_paths=self.material_paths, daily_minutes=self.daily_minutes_spin.value(),
        )
        self._worker.done.connect(self._on_plan_generated)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_plan_generated(self, plan):
        self._set_busy(False)
        self.current_plan = plan
        self._save_stored_config(plan)

        self.table.setRowCount(0)
        for session in plan["sessions"]:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(session["date"].isoformat()))
            self.table.setItem(row, 1, QTableWidgetItem(session["label"]))

        noun = "work" if plan["field"] in WORK_FIELD_LABELS else "study"
        summary = (
            f"Estimated {plan['total_minutes']} minutes total -> {plan['days_needed']} day(s) of "
            f"{noun} before the {plan['exam_date'].isoformat()} deadline."
        )
        if plan["warning"]:
            summary += f"\n{plan['warning']}"
        self.status_label.setText(summary)
        self.commit_button.setEnabled(bool(plan["sessions"]))

    # ---- committing the plan ----

    def _on_commit_clicked(self):
        if not self.current_plan or not self.current_plan["sessions"]:
            return
        self.status_label.setText("Adding to calendar...")
        self._set_busy(True)
        self._worker = CommitPlanWorker(self.planner, self.current_plan["sessions"])
        self._worker.done.connect(self._on_committed)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_committed(self, count):
        self._set_busy(False)
        self.status_label.setText(f"Added {count} session(s) to the calendar.")
        self.commit_button.setEnabled(False)
