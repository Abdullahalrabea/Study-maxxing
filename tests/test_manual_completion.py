import threading
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtTest import QTest, QSignalSpy
from PyQt6.QtWidgets import QApplication

import test_session_picker as fixtures
import test_lockdown_slides as viewer_fixtures
from state_machine import StudyTimerWidget


class ManualCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test-manual-completion"])

    def setUp(self):
        fixtures.SessionPickerTests.setUp(self)
        self.planner = self.widget._study_planner
        self.planner.mark_session_completed = Mock(return_value=True)
        self.widget._refresh_todays_material_and_debt()

    def tearDown(self):
        worker = self.widget._complete_worker
        if worker is not None:
            worker.wait(3000)
            self.app.processEvents()
        fixtures.SessionPickerTests.tearDown(self)

    def wait_for_completion(self):
        for _ in range(200):
            QTest.qWait(10)
            if self.widget._complete_worker is None:
                return
        self.fail("Completion worker did not finish")

    def start_session(self):
        self.widget.total_minutes_spin.setValue(150)
        with patch.object(self.widget, "_open_lockdown"):
            self.widget._start_focus_session()
            self.widget._begin_lockdown_flow()
        self.widget._update_completion_controls()

    def test_button_completes_only_the_selected_session_without_starting_a_camera(self):
        self.widget.daily_session_combo.setCurrentIndex(1)
        with patch("session_snapshot.clear_debt") as clear_debt:
            self.widget.complete_session_button.click()
            self.wait_for_completion()
            clear_debt.assert_called_once_with("A", "Quiz")
        self.planner.mark_session_completed.assert_called_once_with("A", "Quiz", fixtures.TODAY, entry_id="second")
        self.assertIsNone(self.widget._thread)
        self.assertEqual(self.widget.timer_widget.phase, "not_started")
        self.assertEqual(self.widget.daily_session_combo.count(), 1)
        self.assertEqual(self.widget.daily_session_combo.itemData(0)["calendar_entry_id"], "first")
        self.assertIn("Completed in Notion", self.widget.completion_status_label.text())
        self.assertFalse(self.widget.complete_session_button.isEnabled())
        self.widget.complete_session_button.click()
        self.planner.mark_session_completed.assert_called_once()

    def test_manual_completion_finishes_all_blocks_and_stops_monitoring(self):
        self.start_session()
        self.widget.timer_widget.time_left -= 120
        thread = self.widget._thread
        self.widget.complete_session_button.click()
        self.wait_for_completion()
        self.assertEqual(self.widget.timer_widget.phase, "done")
        self.assertEqual(self.widget.timer_widget.study_seconds_elapsed(), 120)
        self.assertFalse(self.widget.timer_widget.timer.isActive())
        thread.request_stop.assert_called_once()
        self.assertFalse(self.widget.resume_lockdown_button.isVisible())
        self.assertFalse(self.widget.choose_session_button.isEnabled())
        self.widget._on_monitor_stopped()
        self.sessions = self.sessions[1:]
        self.widget._choose_session()
        self.assertEqual(self.widget.timer_widget.phase, "not_started")
        self.assertEqual(self.widget.daily_session_combo.currentData()["calendar_entry_id"], "second")

    def test_failed_save_is_visible_and_can_be_retried_after_the_session_ends(self):
        self.start_session()
        self.planner.mark_session_completed.side_effect = [RuntimeError("Network unavailable"), True]
        self.widget.complete_session_button.click()
        self.wait_for_completion()
        self.assertEqual(self.widget.timer_widget.phase, "done")
        self.assertIn("Network unavailable", self.widget.completion_status_label.text())
        self.assertIn("retry", self.widget.completion_status_label.text())
        self.assertTrue(self.widget.complete_session_button.isEnabled())
        self.widget.complete_session_button.click()
        self.wait_for_completion()
        self.assertEqual(self.planner.mark_session_completed.call_count, 2)
        self.assertIn("Completed in Notion", self.widget.completion_status_label.text())

    def test_in_flight_completion_cannot_be_duplicated_or_replaced_by_another_session(self):
        release = threading.Event()
        self.planner.mark_session_completed.side_effect = lambda *args, **kwargs: release.wait(2)
        try:
            self.widget.complete_session_button.click()
            worker = self.widget._complete_worker
            self.assertIsNotNone(worker)
            self.widget._on_manual_session_completed()
            self.widget._start_focus_session()
            self.assertIs(self.widget._complete_worker, worker)
            self.assertEqual(self.widget.timer_widget.phase, "not_started")
            self.assertFalse(self.widget.choose_session_button.isEnabled())
            self.assertFalse(self.widget.daily_session_combo.isEnabled())
        finally:
            release.set()
        self.wait_for_completion()
        self.planner.mark_session_completed.assert_called_once()

    def test_missing_notion_entry_is_reported_without_completing_a_different_entry(self):
        self.planner.mark_session_completed.return_value = False
        self.widget.complete_session_button.click()
        self.wait_for_completion()
        self.assertIn("wasn't found", self.widget.completion_status_label.text())
        self.assertEqual(self.widget.daily_session_combo.count(), 2)
        self.assertTrue(self.widget.complete_session_button.isEnabled())

    def test_loading_cannot_be_marked_completed_and_custom_study_can_finish_locally(self):
        self.widget._start_focus_session()
        self.widget._on_manual_session_completed()
        self.planner.mark_session_completed.assert_not_called()
        self.assertEqual(self.widget.timer_widget.phase, "loading")
        self.widget.timer_widget.begin_study_countdown()
        self.widget._current_session_scheduled = None
        self.widget._on_manual_session_completed()
        self.assertEqual(self.widget.timer_widget.phase, "done")
        self.assertEqual(self.widget.completion_status_label.text(), "Session completed.")
        self.planner.mark_session_completed.assert_not_called()

    def test_lockdown_completion_button_exits_and_saves_the_current_session(self):
        self.start_session()
        display = viewer_fixtures.LockdownSlideTests("test_all_150_slides_are_accessible_and_last_slide_renders")
        display.setUpClass()
        display.setUp()
        try:
            self.widget._lockdown = display.window
            self.widget._last_pdf_path = display.window._pdf_path
            display.window._timer_widget = self.widget.timer_widget
            display.window.completion_requested.connect(self.widget._on_manual_session_completed)
            with patch("engagement_log.log_session") as log:
                display.window.complete_session_button.click()
                self.wait_for_completion()
                log.assert_called_once()
            self.assertIsNone(self.widget._lockdown)
            self.assertEqual(self.widget.timer_widget.phase, "done")
            self.assertTrue(display.window._session_stopped)
            self.assertIn("Completed in Notion", self.widget.completion_status_label.text())
        finally:
            display.tearDown()
            display.doCleanups()


class ManualTimerCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test-manual-timer-completion"])

    def test_finishing_during_study_or_break_preserves_elapsed_time_and_emits_once(self):
        for phase, expected in (("study", 120), ("awaiting_break", 3000), ("break", 3000)):
            with self.subTest(phase=phase):
                timer = StudyTimerWidget()
                timer.set_total_minutes(150)
                timer.phase = phase
                timer.time_left = 2880 if phase == "study" else 480
                timer._paused_for_distraction = True
                timer._paused_for_lockdown_exit = True
                completed = QSignalSpy(timer.finished)
                self.assertTrue(timer.complete_session())
                self.assertFalse(timer.complete_session())
                self.assertEqual(len(completed), 1)
                self.assertEqual(timer.study_seconds_elapsed(), expected)
                timer.resume_after_distraction()
                timer.resume_after_lockdown_exit()
                self.assertFalse(timer.timer.isActive())
                self.assertTrue(timer.reset_for_new_session(20))
                self.assertEqual(timer.study_seconds_elapsed(), 0)
                timer.deleteLater()


if __name__ == "__main__":
    unittest.main()
