import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PyQt6.QtWidgets import QApplication
import fitz

import test_material_library as fixtures
from material_library import MaterialLibrary
from main_window import FocusSessionWidget
from study_planner import StudyPlanner
import study_planner_panel as panel_module

TODAY = fixtures.TODAY


class SessionPickerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test-session-picker"])

    def setUp(self):
        fixtures.MaterialLibraryTests.setUp(self)
        self.patchers = [
            patch.object(panel_module, "MaterialLibrary", return_value=self.library),
            patch("main_window.date"),
            patch("session_snapshot.load_debt", return_value=None),
            patch("session_snapshot.clear_debt"),
            patch("main_window.VisionMonitorThread"),
            patch("topic_preview.TopicPreviewDialog"),
        ]
        results = [patcher.start() for patcher in self.patchers]
        results[1].today.return_value = TODAY
        with patch.object(FocusSessionWidget, "_refresh_penalty_banner"):
            self.widget = FocusSessionWidget()
        paths = self.library.import_files("A", [
            fixtures.make_pdf(self.root / "one.pdf", "First chapter"),
            fixtures.make_pdf(self.root / "two.pdf", "Second chapter"),
        ])
        self.library.set_chapters("A", paths[0], "1")
        self.library.set_chapters("A", paths[1], "2")
        self.sessions = [
            {"course": "A", "field": "Quiz", "label": "Study A Quiz chap 1", "chapter_range": [1, 1],
             "calendar_entry_id": "first", "calendar_date": TODAY.isoformat()},
            {"course": "A", "field": "Quiz", "label": "Study A Quiz chap 2", "chapter_range": [2, 2],
             "calendar_entry_id": "second", "calendar_date": TODAY.isoformat()},
        ]
        self.widget._study_planner = SimpleNamespace(
            todays_sessions=lambda today: self.sessions, llm=Mock(), list_exams=Mock(return_value=[])
        )

    def tearDown(self):
        self.widget._day_timer.stop()
        self.widget.timer_widget.timer.stop()
        if hasattr(self.widget, "_session_menu"):
            self.widget._session_menu.close()
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()
        for patcher in reversed(self.patchers):
            patcher.stop()
        fixtures.MaterialLibraryTests.tearDown(self)

    def choose_second(self):
        self.widget.choose_session_button.click()
        actions = [action for action in self.widget._session_menu.actions() if action.isCheckable()]
        self.assertEqual(len(actions), 2)
        actions[1].trigger()

    def test_button_lists_both_sessions_and_selects_the_second_chapters_slides(self):
        self.choose_second()
        self.assertEqual(self.widget.daily_session_combo.currentData()["calendar_entry_id"], "second")
        with fitz.open(self.widget._todays_material[0]) as document:
            self.assertIn("Second chapter", document[0].get_text())
            self.assertNotIn("First chapter", document[0].get_text())

    def test_selected_session_survives_a_reordered_notion_refresh_before_lockdown(self):
        self.choose_second()
        self.sessions.reverse()
        with patch.object(self.widget, "_open_lockdown") as open_lockdown:
            self.widget._start_focus_session()
            self.assertFalse(self.widget.choose_session_button.isEnabled())
            self.widget._begin_lockdown_flow()
            self.assertEqual(self.widget._current_session_scheduled["calendar_entry_id"], "second")
            pdf = open_lockdown.call_args.args[0]
            with fitz.open(pdf) as document:
                self.assertIn("Second chapter", document[0].get_text())

    def test_session_deleted_in_notion_cannot_start_a_different_session_silently(self):
        self.choose_second()
        self.sessions = self.sessions[:1]
        with patch.object(self.widget.timer_widget, "start_clicked") as start:
            self.widget._start_focus_session()
            start.assert_not_called()
        self.assertIn("Choose a session again", self.widget.material_status_label.text())

    def test_selected_session_remains_selected_when_its_notion_title_changes(self):
        self.choose_second()
        self.sessions[1] = {**self.sessions[1], "label": "Study A Quiz chap 1", "chapter_range": [1, 1]}
        self.widget._refresh_todays_material_and_debt()
        self.assertEqual(self.widget.daily_session_combo.currentData()["calendar_entry_id"], "second")
        with fitz.open(self.widget._todays_material[0]) as document:
            self.assertIn("First chapter", document[0].get_text())

    def test_choose_another_session_after_completing_the_first_without_restarting(self):
        self.widget._refresh_todays_material_and_debt()
        self.widget.total_minutes_spin.setValue(10)
        self.widget._start_focus_session()
        self.widget.timer_widget.begin_study_countdown()
        self.widget.timer_widget.finish_block_early()
        self.assertEqual(self.widget.timer_widget.phase, "done")
        self.assertFalse(self.widget.choose_session_button.isEnabled())
        self.widget._on_monitor_stopped()
        self.sessions = self.sessions[1:]
        self.widget.choose_session_button.click()
        self.assertEqual(self.widget.timer_widget.phase, "not_started")
        self.assertEqual(self.widget.daily_session_combo.currentData()["calendar_entry_id"], "second")
        self.assertTrue(self.widget.timer_widget.start_button.isEnabled())
        self.widget._session_menu.close()
        self.widget.total_minutes_spin.setValue(20)
        self.widget._start_focus_session()
        self.assertEqual(self.widget.timer_widget.phase, "loading")
        self.assertEqual(self.widget.timer_widget.time_left, 1200)

    def test_active_session_cannot_be_reset_by_the_picker(self):
        self.widget._refresh_todays_material_and_debt()
        self.widget._start_focus_session()
        self.widget._choose_session()
        self.assertEqual(self.widget.timer_widget.phase, "loading")
        self.assertFalse(self.widget.choose_session_button.isEnabled())

    def test_no_calendar_shows_a_clear_message(self):
        self.widget._study_planner = None
        self.widget.choose_session_button.click()
        self.assertIn("Connect your Notion calendar", self.widget.material_status_label.text())


class CalendarSessionIdentityTests(unittest.TestCase):
    def planner(self, rows):
        planner = object.__new__(StudyPlanner)
        planner.calendar_database_id = "calendar"
        planner.list_exams = Mock(return_value=[{"course": "A", "field": "Quiz", "date": TODAY + timedelta(days=5)}])
        planner.notion_client = SimpleNamespace(
            list_calendar_entries=lambda _: rows, delete_calendar_entry=Mock()
        )
        return planner

    def test_distinct_chapters_and_times_remain_selectable_while_exact_repeats_are_combined(self):
        rows = [
            {"id": "first", "date": TODAY.isoformat(), "title": "Study A Quiz chap 1"},
            {"id": "second", "date": TODAY.isoformat(), "title": "Study A Quiz chap 2"},
            {"id": "repeat", "date": TODAY.isoformat(), "title": "Study A Quiz chap 1"},
            {"id": "morning", "date": TODAY.isoformat() + "T09:00:00+03:00", "title": "Study A Quiz Review"},
            {"id": "evening", "date": TODAY.isoformat() + "T18:00:00+03:00", "title": "Study A Quiz Review"},
        ]
        sessions = self.planner(rows).todays_sessions(today=TODAY)
        self.assertEqual([session["calendar_entry_id"] for session in sessions], ["first", "second", "morning", "evening"])
        self.assertIn("18:00", FocusSessionWidget._session_text(sessions[-1]))

    def test_completion_archives_only_the_selected_notion_row(self):
        planner = self.planner([
            {"id": "first", "date": TODAY.isoformat(), "title": "Study A Quiz chap 1"},
            {"id": "second", "date": TODAY.isoformat(), "title": "Study A Quiz chap 2"},
        ])
        self.assertTrue(planner.mark_session_completed("A", "Quiz", TODAY, entry_id="second"))
        planner.notion_client.delete_calendar_entry.assert_called_once_with("second")
        planner.notion_client.delete_calendar_entry.reset_mock()
        self.assertFalse(planner.mark_session_completed("A", "Quiz", TODAY, entry_id="deleted"))
        planner.notion_client.delete_calendar_entry.assert_not_called()

    def test_selected_work_session_can_be_completed(self):
        planner = self.planner([{"id": "project", "date": TODAY.isoformat(), "title": "Work: A Project work session"}])
        self.assertTrue(planner.mark_session_completed("A", "Project", TODAY, entry_id="project"))
        planner.notion_client.delete_calendar_entry.assert_called_once_with("project")


if __name__ == "__main__":
    unittest.main()
