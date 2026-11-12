import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from datetime import date
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
for folder in ("core", "ui", "ui/games", "services", "AI", "vision"):
    sys.path.insert(0, str(ROOT / folder))

from PyQt6.QtCore import QSettings, QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication

import study_planner_panel as module
from material_library import MaterialLibrary


class DelayedListWorker(QThread):
    """Keep run() alive after its result is delivered to the GUI thread."""

    done = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, planner):
        super().__init__()
        self.planner = planner

    def run(self):
        self.done.emit(self.planner.list_exams())
        self.planner.release_list.wait(5)


class FakePlanner:
    def __init__(self):
        self.exams = [
            {"course": "CS 301", "field": "Midterm", "date": date(2026, 11, 20)},
            {"course": "CS 302", "field": "Final", "date": date(2026, 12, 10)},
        ]
        self.release_list = threading.Event()
        self.release_generation = threading.Event()
        self.generations = []
        self.fail_generation = False

    def list_exams(self):
        return self.exams

    def generate_plan(self, course, field, **kwargs):
        self.generations.append((course, field, kwargs))
        self.release_generation.wait(5)
        if self.fail_generation:
            raise module.StudyPlannerError("Server unavailable")
        exam = next(e for e in self.exams if (e["course"], e["field"]) == (course, field))
        return {
            "course": course,
            "field": field,
            "exam_date": exam["date"],
            "total_minutes": 60,
            "days_needed": 1,
            "sessions": [{"date": date(2026, 11, 10), "label": "Study"}],
            "warning": "",
            "topics": ["Saved topic"],
        }


class StudyPlannerStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = QSettings(
            str(Path(self.temp_dir.name) / "settings.ini"), QSettings.Format.IniFormat
        )
        self.planner = FakePlanner()
        self.panel = None
        self.patchers = [
            patch.object(module, "StudyPlanner", return_value=self.planner),
            patch.object(module, "QSettings", return_value=self.settings),
            patch.object(module, "ListExamsWorker", DelayedListWorker),
            patch.object(module, "MaterialLibrary", return_value=MaterialLibrary(Path(self.temp_dir.name) / "materials")),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        self.planner.release_list.set()
        self.planner.release_generation.set()
        if self.panel is not None:
            for worker in tuple(self.panel._active_workers):
                worker.wait(5000)
            self.app.processEvents()
            self.panel.close()
            self.panel.deleteLater()
            self.app.processEvents()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.settings.sync()
        self.temp_dir.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Timed out waiting for the planner")

    def save_config(self, course="CS 301", field="Midterm", last_date="2026-11-19"):
        self.settings.setValue(module._config_key(course, field), json.dumps({
            "chapter_count": 3,
            "daily_minutes": 45,
            "material_paths": ["notes.pdf"],
            "last_exam_date": last_date,
        }))

    def open_panel(self):
        self.panel = module.StudyPlannerPanel("token", "table", "calendar")
        return self.panel

    def test_changed_saved_date_generates_once_and_retains_running_list_worker(self):
        self.save_config()
        panel = self.open_panel()
        list_worker = panel._worker
        self.wait_for(lambda: len(self.planner.generations) == 1)

        self.assertTrue(list_worker.isRunning())
        self.assertIn(list_worker, panel._active_workers)
        self.assertEqual(len(panel._active_workers), 2)
        self.assertEqual(panel.chapter_spin.value(), 3)
        self.assertEqual(self.planner.generations[0][2]["daily_minutes"], 45)

        # Duplicate actions during an operation must not replace its thread.
        generation_worker = panel._worker
        panel._on_generate_clicked()
        panel._reload_exams()
        panel._on_commit_clicked()
        self.assertIs(panel._worker, generation_worker)
        self.assertEqual(len(self.planner.generations), 1)

        self.planner.release_list.set()
        self.planner.release_generation.set()
        self.wait_for(lambda: not panel._active_workers)
        self.assertEqual(len(self.planner.generations), 1)
        self.assertFalse(panel._busy)
        self.assertEqual(panel.table.rowCount(), 1)
        self.assertTrue(panel.commit_button.isEnabled())
        stored = json.loads(self.settings.value(module._config_key("CS 301", "Midterm")))
        self.assertEqual(stored["last_exam_date"], "2026-11-20")

    def test_unchanged_date_restores_config_without_generating(self):
        self.save_config(last_date="2026-11-20")
        self.planner.release_list.set()
        panel = self.open_panel()
        self.wait_for(lambda: not panel._active_workers)
        self.assertEqual(self.planner.generations, [])
        self.assertEqual(panel.chapter_spin.value(), 3)
        self.assertEqual(panel.material_paths, ["notes.pdf"])
        self.assertTrue(panel.generate_button.isEnabled())

    def test_switching_course_regenerates_selected_deadline_once(self):
        self.save_config(course="CS 302", field="Final", last_date="2026-12-09")
        self.planner.release_list.set()
        panel = self.open_panel()
        self.wait_for(lambda: not panel._active_workers)
        panel.course_combo.setCurrentIndex(1)
        self.wait_for(lambda: len(self.planner.generations) == 1)
        self.assertEqual(self.planner.generations[0][:2], ("CS 302", "Final"))
        self.planner.release_generation.set()
        self.wait_for(lambda: not panel._active_workers)
        self.assertEqual(len(self.planner.generations), 1)

    def test_failed_generation_allows_retry(self):
        self.save_config()
        self.planner.fail_generation = True
        self.planner.release_list.set()
        self.planner.release_generation.set()
        panel = self.open_panel()
        self.wait_for(lambda: not panel._active_workers)
        self.assertIn("Server unavailable", panel.status_label.text())
        self.assertTrue(panel.generate_button.isEnabled())
        self.planner.fail_generation = False
        panel._on_generate_clicked()
        self.wait_for(lambda: not panel._active_workers)
        self.assertEqual(len(self.planner.generations), 2)
        self.assertTrue(panel.commit_button.isEnabled())

    def test_empty_schedule_clears_previous_deadlines(self):
        self.planner.release_list.set()
        panel = self.open_panel()
        self.wait_for(lambda: not panel._active_workers)
        self.planner.exams = []
        panel._reload_exams()
        self.wait_for(lambda: not panel._active_workers)
        self.assertEqual(panel.course_combo.count(), 0)
        self.assertEqual(panel.field_combo.count(), 0)
        self.assertIn("No courses", panel.status_label.text())

    def test_uploads_are_saved_before_generation_and_restored_for_each_course(self):
        import fitz
        root = Path(self.temp_dir.name)
        paths = []
        for name, text in (("a.pdf", "Course A"), ("b.pdf", "Course B")):
            path = root / name
            with fitz.open() as document:
                document.new_page().insert_text((72, 72), text)
                document.save(str(path))
            paths.append(str(path))
        self.planner.release_list.set()
        panel = self.open_panel()
        self.wait_for(lambda: not panel._active_workers)
        with patch.object(module.QFileDialog, "getOpenFileNames", return_value=([paths[0]], "")):
            panel._on_attach_clicked()
        first = json.loads(self.settings.value(module._config_key("CS 301", "Midterm")))["material_paths"]
        self.assertEqual(len(first), 1)
        self.assertNotEqual(first[0], paths[0])
        panel.course_combo.setCurrentIndex(1)
        with patch.object(module.QFileDialog, "getOpenFileNames", return_value=([paths[1]], "")):
            panel._on_attach_clicked()
        second = list(panel.material_paths)
        for original in paths:
            Path(original).unlink()
        panel.course_combo.setCurrentIndex(0)
        self.assertEqual(panel.material_paths, first)
        panel.course_combo.setCurrentIndex(1)
        self.assertEqual(panel.material_paths, second)
        self.assertTrue(all(Path(path).is_file() for path in first + second))
        self.assertEqual(self.planner.generations, [])

    def test_switching_courses_cannot_commit_the_previous_courses_preview(self):
        self.planner.release_list.set()
        self.planner.release_generation.set()
        panel = self.open_panel()
        self.wait_for(lambda: not panel._active_workers)
        panel._on_generate_clicked()
        self.wait_for(lambda: not panel._active_workers)
        self.assertTrue(panel.commit_button.isEnabled())
        panel.course_combo.setCurrentIndex(1)
        self.assertIsNone(panel.current_plan)
        self.assertFalse(panel.commit_button.isEnabled())

    def test_removed_material_does_not_return_when_switching_back_to_a_saved_course(self):
        import fitz
        path = Path(self.temp_dir.name) / "remove.pdf"
        with fitz.open() as document:
            document.new_page().insert_text((72, 72), "Course notes")
            document.save(str(path))
        self.planner.release_list.set()
        panel = self.open_panel()
        self.wait_for(lambda: not panel._active_workers)
        with patch.object(module.QFileDialog, "getOpenFileNames", return_value=([str(path)], "")):
            panel._on_attach_clicked()
        uploaded = list(panel.material_paths)
        panel.course_combo.setCurrentIndex(1)
        panel.material_library.remove_files("CS 301", uploaded)
        panel.course_combo.setCurrentIndex(0)
        self.assertEqual(panel.material_paths, [])
        stored = json.loads(self.settings.value(module._config_key("CS 301", "Midterm")))
        self.assertEqual(stored["material_paths"], [])


if __name__ == "__main__":
    unittest.main()
