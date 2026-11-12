import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
for folder in ("core", "ui", "ui/games", "services", "AI", "vision"):
    sys.path.insert(0, str(ROOT / folder))

import fitz
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication
from material_library import MaterialLibrary, MaterialError
import study_planner_panel as panel_module
from study_planner import StudyPlanner, _match_label_to_deadline
from main_window import FocusSessionWidget

TODAY = date(2026, 11, 9)


def make_pdf(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), text)
        document.save(str(path))
    return str(path)


class MaterialLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = MaterialLibrary(self.root / "library")
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        self.setting_patch = patch.object(panel_module, "QSettings", return_value=self.settings)
        self.setting_patch.start()

    def tearDown(self):
        self.setting_patch.stop()
        self.settings.sync()
        self.temp.cleanup()

    def test_uploads_survive_original_deletion_and_restart_with_same_names_across_courses(self):
        a = make_pdf(self.root / "downloads-a" / "notes.pdf", "Course A")
        b = make_pdf(self.root / "downloads-b" / "notes.pdf", "Course B")
        saved_a = self.library.import_files("Course A", [a])
        saved_b = self.library.import_files("Course B", [b])
        Path(a).unlink()
        Path(b).unlink()
        restarted = MaterialLibrary(self.library.root)
        self.assertEqual(restarted.resolve("Course A")[0], saved_a)
        self.assertEqual(restarted.resolve("Course B")[0], saved_b)
        self.assertNotEqual(saved_a, saved_b)
        with fitz.open(saved_a[0]) as document:
            self.assertIn("Course A", document[0].get_text())

    def test_repeated_uploads_append_without_discarding_or_duplicating_files(self):
        first = make_pdf(self.root / "first.pdf", "First chapter")
        second = make_pdf(self.root / "second.pdf", "Second chapter")
        self.library.import_files("A", [first])
        self.library.import_files("A", [second, first])
        self.assertEqual([item["name"] for item in self.library.materials("A")], ["first.pdf", "second.pdf"])

    def test_reuploading_an_updated_file_replaces_its_old_version(self):
        first = make_pdf(self.root / "old" / "notes.pdf", "Old contents")
        newer = make_pdf(self.root / "new" / "notes.pdf", "New contents")
        old_managed = self.library.import_files("A", [first])
        self.library.set_chapters("A", old_managed[0], "2")
        updated = self.library.import_files("A", [newer])
        resolved, _ = self.library.resolve("A", old_managed + [first])
        self.assertEqual(resolved, updated)
        self.assertEqual(len(self.library.materials("A")), 1)
        self.assertEqual(self.library.materials("A")[0]["chapters"], [2, 2])

    def test_all_documents_are_in_pdf_packet_with_file_navigation(self):
        paths = self.library.import_files("A", [
            make_pdf(self.root / "one.pdf", "First document"),
            make_pdf(self.root / "two.pdf", "Second document"),
        ])
        packet = self.library.prepare_pdf("A", paths)
        with fitz.open(packet) as document:
            self.assertEqual(len(document), 2)
            self.assertIn("First document", document[0].get_text())
            self.assertIn("Second document", document[1].get_text())
            self.assertEqual(document.get_toc(), [[1, "one.pdf", 1], [1, "two.pdf", 2]])
        self.assertEqual(packet, self.library.prepare_pdf("A", paths))

    def test_text_word_and_images_can_be_studied_in_the_pdf_viewer(self):
        from docx import Document
        from PIL import Image
        text = self.root / "notes.txt"
        text.write_text("Persistent text notes " * 1500, encoding="utf-8")
        word_path = self.root / "summary.docx"
        word = Document()
        word.add_paragraph("Word course summary")
        word.save(word_path)
        image_path = self.root / "diagram.png"
        Image.new("RGB", (120, 80), "blue").save(image_path)
        paths = self.library.import_files("A", [str(text), str(word_path), str(image_path)])
        packet = self.library.prepare_pdf("A", paths)
        with fitz.open(packet) as document:
            self.assertGreater(len(document), 3)
            self.assertIn("Word course summary", "".join(page.get_text() for page in document))
            self.assertEqual(len(document.get_toc()), 3)

    def test_chapter_assignments_select_matching_files_and_keep_unassigned_notes(self):
        paths = self.library.import_files("A", [
            make_pdf(self.root / "one.pdf", "Chapter 1"),
            make_pdf(self.root / "two.pdf", "Chapter 2"),
            make_pdf(self.root / "overview.pdf", "Shared notes"),
        ])
        self.library.set_chapters("A", paths[0], "1")
        self.library.set_chapters("A", paths[1], "2-3")
        self.assertEqual(self.library.resolve("A", chapter_range=[2, 2])[0], paths[1:])
        with self.assertRaises(MaterialError):
            self.library.set_chapters("A", paths[0], "3-1")

    def test_daily_lookup_changes_course_on_the_next_day_and_merges_its_files(self):
        self.library.import_files("A", [make_pdf(self.root / "a.pdf", "Course A")])
        self.library.import_files("B", [make_pdf(self.root / "b1.pdf", "B one"),
                                        make_pdf(self.root / "b2.pdf", "B two")])
        planner = SimpleNamespace(todays_sessions=lambda today: [
            {"course": "A" if today == TODAY else "B", "field": "Quiz", "date": TODAY + timedelta(days=5)}
        ])
        for day, course, pages in ((TODAY, "A", 1), (TODAY + timedelta(days=1), "B", 2)):
            pdf, _, session = panel_module.find_todays_material(planner, today=day, library=self.library)
            self.assertEqual(session["course"], course)
            with fitz.open(pdf) as document:
                self.assertEqual(len(document), pages)
                if course == "B":
                    self.assertNotIn("Course A", "".join(page.get_text() for page in document))

    def test_missing_scheduled_material_never_falls_back_to_another_course(self):
        self.library.import_files("B", [make_pdf(self.root / "b.pdf", "Wrong course")])
        sessions = [{"course": "A", "field": "Quiz"}, {"course": "B", "field": "Quiz"}]
        planner = SimpleNamespace(todays_sessions=lambda today: sessions)
        pdf, _, session = panel_module.find_todays_material(planner, today=TODAY, library=self.library)
        self.assertIsNone(pdf)
        self.assertEqual(session["course"], "A")

    def test_legacy_upload_is_copied_to_library_and_remains_available_after_move(self):
        original = make_pdf(self.root / "old.pdf", "Older saved plan")
        self.settings.setValue(panel_module._config_key("A", "Quiz"), json.dumps({"material_path": original}))
        session = {"course": "A", "field": "Quiz"}
        first, _, _ = panel_module.find_todays_material(None, session=session, library=self.library)
        Path(original).unlink()
        second, _, _ = panel_module.find_todays_material(None, session=session, library=self.library)
        self.assertEqual(first, second)
        self.assertTrue(Path(second).is_file())

    def test_calendar_retains_day_and_chapters_and_avoids_partial_deadline_match(self):
        exams = [{"course": "A", "field": "Quiz-1", "date": TODAY + timedelta(days=5)},
                 {"course": "B", "field": "Final", "date": TODAY + timedelta(days=10)}]
        planner = object.__new__(StudyPlanner)
        planner.list_exams = Mock(return_value=exams)
        planner.notion_client = SimpleNamespace(list_calendar_entries=lambda _: [
            {"date": TODAY.isoformat(), "title": "Study B Final chap 2 & 3"},
            {"date": TODAY.isoformat(), "title": "Study A Quiz-1 chap 1"},
            {"date": TODAY.isoformat(), "title": "Study A Quiz-10 chap 9"},
            {"date": (TODAY + timedelta(days=1)).isoformat(), "title": "Study A Quiz-1 chap 3"},
        ])
        planner.calendar_database_id = "calendar"
        sessions = planner.todays_sessions(today=TODAY)
        self.assertEqual([session["course"] for session in sessions], ["A", "B"])
        self.assertEqual(sessions[1]["chapter_range"], [2, 3])
        self.assertEqual(sessions[0]["session_date"], TODAY)
        self.assertIsNone(_match_label_to_deadline("Study A Quiz-10 chap 9", exams))


class FocusMaterialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        MaterialLibraryTests.setUp(self)
        self.library_patch = patch.object(panel_module, "MaterialLibrary", return_value=self.library)
        self.library_patch.start()
        self.date_patch = patch("main_window.date")
        self.mock_date = self.date_patch.start()
        self.mock_date.today.return_value = TODAY
        with patch.object(FocusSessionWidget, "_refresh_penalty_banner"):
            self.widget = FocusSessionWidget()
        self.sessions = [{"course": "A", "field": "Quiz", "label": "Study A Quiz chap 1"},
                         {"course": "B", "field": "Quiz", "label": "Study B Quiz chap 1"}]
        self.widget._study_planner = SimpleNamespace(todays_sessions=lambda today: self.sessions)
        self.library.import_files("A", [make_pdf(self.root / "a.pdf", "A notes")])
        self.library.import_files("B", [make_pdf(self.root / "b.pdf", "B notes")])

    def tearDown(self):
        self.widget._day_timer.stop()
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()
        self.date_patch.stop()
        self.library_patch.stop()
        MaterialLibraryTests.tearDown(self)

    def test_refresh_does_not_add_debt_twice_and_switches_to_selected_course(self):
        def debt(course, field):
            return {"shortfall_seconds": 600 if course == "A" else 1200}
        with patch("session_snapshot.load_debt", side_effect=debt):
            self.widget._refresh_todays_material_and_debt()
            self.assertEqual(self.widget.total_minutes_spin.value(), 110)
            self.widget._refresh_todays_material_and_debt()
            self.assertEqual(self.widget.total_minutes_spin.value(), 110)
            self.widget.daily_session_combo.setCurrentIndex(1)
            self.assertEqual(self.widget._todays_material[2]["course"], "B")
            self.assertEqual(self.widget.total_minutes_spin.value(), 120)
            self.widget._refresh_todays_material_and_debt()
            self.assertEqual(self.widget.total_minutes_spin.value(), 120)

    def test_start_refreshes_new_uploads_and_keeps_the_selected_course(self):
        with patch("session_snapshot.load_debt", return_value=None):
            self.widget._refresh_todays_material_and_debt()
            self.widget.daily_session_combo.setCurrentIndex(1)
            self.library.import_files("B", [make_pdf(self.root / "extra.pdf", "Extra B notes")])
            self.widget.timer_widget.start_clicked = Mock()
            self.widget._start_focus_session()
            self.widget.timer_widget.start_clicked.assert_called_once()
            self.assertEqual(self.widget._todays_material[2]["course"], "B")
            with fitz.open(self.widget._todays_material[0]) as document:
                self.assertEqual(len(document), 2)

    def test_midnight_refresh_uses_the_new_day_schedule(self):
        with patch("session_snapshot.load_debt", return_value=None):
            self.widget._refresh_todays_material_and_debt()
            self.sessions = self.sessions[1:]
            self.mock_date.today.return_value = TODAY + timedelta(days=1)
            self.widget._check_material_day()
            self.assertEqual(self.widget._todays_material[2]["course"], "B")
            self.assertEqual(self.widget._material_day, TODAY + timedelta(days=1))


if __name__ == "__main__":
    unittest.main()
