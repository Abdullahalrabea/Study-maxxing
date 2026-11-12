import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PyQt6.QtWidgets import QApplication, QGraphicsOpacityEffect, QLabel

import test_material_library as fixtures
import gaze_engine as engine
from main_window import FocusSessionWidget
import main_window
from lockdown_window import LockdownWindow, DIMMED_OPACITY


class DistractionModeTests(unittest.TestCase):
    def run_monitor(self, centered, phones, times, *, suppressed=False, challenges=None):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            for name, constant in (("face", "MODEL_PATH"), ("phone", "PHONE_MODEL_PATH"),
                                   ("hand", "HAND_MODEL_PATH"), ("body", "PUSHUP_MODEL_PATH")):
                path = root / name
                path.write_bytes(b"test model")
                stack.enter_context(patch.object(engine, constant, path))
            if challenges is not None:
                stack.enter_context(patch.object(engine, "POSE_CHALLENGES_ENABLED", challenges))
            if not engine.POSE_CHALLENGES_ENABLED:
                engine.HAND_MODEL_PATH.unlink()
                engine.PUSHUP_MODEL_PATH.unlink()
            camera = Mock(index=-1)
            def read():
                camera.index += 1
                return True, np.zeros((64, 64, 3), dtype=np.uint8)
            camera.read.side_effect = read
            stack.enter_context(patch.object(engine.cv2, "VideoCapture", return_value=camera))
            face_factory = stack.enter_context(patch.object(engine.FaceLandmarker, "create_from_options"))
            detector = face_factory.return_value.__enter__.return_value
            landmarks = [SimpleNamespace(x=.5, y=.5, z=0)] * 478
            detector.detect_for_video.side_effect = lambda *_: SimpleNamespace(
                face_landmarks=[landmarks] if centered[camera.index] else [],
                face_blendshapes=[], facial_transformation_matrixes=[np.eye(4)],
            )
            stack.enter_context(patch.object(engine.mp_drawing, "draw_landmarks"))
            stack.enter_context(patch.object(engine, "get_gaze_direction", return_value=("Center", .5, .5)))
            phone_factory = stack.enter_context(patch.object(engine, "PhoneDetector"))
            phone = phone_factory.return_value
            phone.get_detections.return_value = []
            phone.is_phone_visible.side_effect = lambda _: phones[camera.index]
            poses = stack.enter_context(patch.object(engine, "PoseChallenge"))
            pose = poses.return_value.load_bank.return_value
            pose.current = SimpleNamespace(name="test reference")
            pose.get_display_frame.return_value = np.zeros((8, 8, 3), dtype=np.uint8)
            pose.check_match.return_value = SimpleNamespace(visible=False, hand_points_norm=[])
            pushups = stack.enter_context(patch.object(engine, "PushupCounter"))
            clock = [0] + [value for timestamp in times for value in (timestamp, timestamp)]
            stack.enter_context(patch.object(engine.time, "time", side_effect=clock))
            events, references = [], []
            penalties = Mock()
            with contextlib.redirect_stdout(io.StringIO()):
                engine.main(
                    should_stop=lambda: camera.index == len(centered) - 1,
                    on_disruption_change=events.append,
                    on_frame=lambda frame, reference: references.append(reference),
                    is_suppressed=lambda: suppressed,
                    on_penalty_pending_change=penalties,
                )
            camera.release.assert_called_once()
            return SimpleNamespace(events=events, references=references, poses=poses, pushups=pushups, penalties=penalties)

    def test_looking_away_pauses_until_refocused_without_a_pose_or_extra_models(self):
        result = self.run_monitor([False, False, False, True, True], [False] * 5, [1, 7, 8, 9, 10.2])
        self.assertEqual(result.events, [True, False])
        self.assertTrue(all(reference is None for reference in result.references))
        result.poses.assert_not_called()
        result.pushups.assert_not_called()
        result.penalties.assert_not_called()

    def test_repeated_phone_distractions_only_pause_and_resume(self):
        times = [timestamp + offset for offset in (0, 20, 40, 60) for timestamp in (1, 7, 8, 9, 10.2)]
        result = self.run_monitor([True] * 20, [True, True, True, False, False] * 4, times)
        self.assertEqual(result.events, [True, False] * 4)
        self.assertTrue(all(reference is None for reference in result.references))
        result.poses.assert_not_called()
        result.pushups.assert_not_called()
        result.penalties.assert_not_called()

    def test_breaks_do_not_trigger_distraction_pauses(self):
        result = self.run_monitor([False] * 3, [True] * 3, [1, 7, 8], suppressed=True)
        self.assertEqual(result.events, [])
        result.poses.assert_not_called()

    def test_challenge_implementation_remains_available_for_explicit_restoration(self):
        result = self.run_monitor([False] * 3, [False] * 3, [1, 7, 8], challenges=True)
        self.assertEqual(result.events, [True])
        result.poses.return_value.load_bank.return_value.pick_random_challenge.assert_called_once()
        self.assertIsNotNone(result.references[-1])

    def test_old_pushup_debt_cannot_start_a_challenge_while_disabled(self):
        with patch.object(engine.cv2, "VideoCapture") as camera:
            with self.assertRaisesRegex(RuntimeError, "temporarily disabled"):
                engine.main(initial_pushups_owed=10)
            camera.assert_not_called()


class DistractionDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test-distraction-pause"])

    def setUp(self):
        with patch.object(FocusSessionWidget, "_refresh_penalty_banner"):
            self.focus = FocusSessionWidget()
        self.focus.timer_widget.session_starting.disconnect(self.focus._on_session_starting)
        self.focus.timer_widget.start_clicked()
        self.focus.timer_widget.begin_study_countdown()
        self.display = SimpleNamespace(
            webcam_label=QLabel(), pose_label=QLabel(), _pdf_opacity_effect=QGraphicsOpacityEffect(),
            _set_pace_frozen=Mock(),
        )
        self.display.set_dimmed = lambda dimmed: LockdownWindow.set_dimmed(self.display, dimmed)
        self.display.set_disrupted = lambda disrupted: LockdownWindow.set_disrupted(self.display, disrupted)
        self.focus._lockdown = self.display

    def tearDown(self):
        self.focus.timer_widget.timer.stop()
        self.focus._day_timer.stop()
        self.focus.close()
        self.focus.deleteLater()
        self.app.processEvents()

    def test_timer_and_slides_stay_paused_and_dark_without_a_pose_image(self):
        self.assertTrue(self.focus.timer_widget.timer.isActive())


        self.focus._on_disruption_changed(True)
        self.assertFalse(self.focus.timer_widget.timer.isActive())
        self.assertAlmostEqual(self.display._pdf_opacity_effect.opacity(), DIMMED_OPACITY)
        LockdownWindow.update_frame(self.display, np.zeros((64, 64, 3), dtype=np.uint8), None)
        self.assertAlmostEqual(self.display._pdf_opacity_effect.opacity(), DIMMED_OPACITY)
        self.assertTrue(self.display.pose_label.isHidden())
        self.focus._on_disruption_changed(False)
        self.assertTrue(self.focus.timer_widget.timer.isActive())
        self.assertEqual(self.display._pdf_opacity_effect.opacity(), 1.0)
        self.assertEqual([call.args[0] for call in self.display._set_pace_frozen.call_args_list], [True, False])

    def test_refocusing_outside_lockdown_does_not_restart_the_timer(self):
        self.focus._on_disruption_changed(True)
        self.assertTrue(self.focus.timer_widget.pause_for_lockdown_exit())
        self.focus._on_disruption_changed(False)
        self.assertFalse(self.focus.timer_widget.timer.isActive())
        self.focus.timer_widget.resume_after_lockdown_exit()
        self.assertTrue(self.focus.timer_widget.timer.isActive())

    def test_resuming_lockdown_while_distracted_keeps_the_timer_paused(self):
        self.focus._on_disruption_changed(True)
        self.assertTrue(self.focus.timer_widget.pause_for_lockdown_exit())
        self.focus.timer_widget.resume_after_lockdown_exit()
        self.assertFalse(self.focus.timer_widget.timer.isActive())
        self.focus._on_disruption_changed(False)
        self.assertTrue(self.focus.timer_widget.timer.isActive())


class PhysicalChallengePromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test-physical-challenges"])

    def test_old_penalty_prompt_is_hidden_and_cannot_launch_pushups(self):
        with patch("session_snapshot.load_penalty", return_value={"reps_owed": 10}) as load_penalty, \
             patch.object(main_window, "PenaltyCompletionDialog") as dialog:
            focus = FocusSessionWidget()
            try:
                self.assertTrue(focus.penalty_banner.isHidden())
                self.assertTrue(focus.penalty_complete_button.isHidden())
                focus._on_penalty_complete_clicked()
                load_penalty.assert_not_called()
                dialog.assert_not_called()
            finally:
                focus._day_timer.stop()
                focus.close()
                focus.deleteLater()
                self.app.processEvents()

    def test_penalty_controls_can_be_restored_only_when_explicitly_enabled(self):
        with patch.object(main_window, "PHYSICAL_CHALLENGES_ENABLED", True), \
             patch("session_snapshot.load_penalty", return_value={"reps_owed": 10}), \
             patch.object(main_window, "PenaltyCompletionDialog") as dialog:
            focus = FocusSessionWidget()
            try:
                self.assertFalse(focus.penalty_banner.isHidden())
                self.assertFalse(focus.penalty_complete_button.isHidden())
                focus._on_penalty_complete_clicked()
                dialog.assert_called_once_with(10, parent=focus)
                dialog.return_value.exec.assert_called_once()
            finally:
                focus._day_timer.stop()
                focus.close()
                focus.deleteLater()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
