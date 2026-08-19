# Push-up rep counter for Study Warden. Tracks the shoulder-elbow-wrist angle
# and counts a rep every time it goes from "up" (arms extended) to "down"
# (arms bent) and back to "up". This is a different job than pose_engine.py's
# reference-photo matching, so it gets its own small PoseLandmarker instance.
#
# Needs, next to this file:
#   vision/pose_landmarker_lite.task
#     https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task
#
# Tip: point the camera so your arms/shoulders are clearly visible while you
# do push-ups (e.g. laptop on the floor, or camera off to the side) -- a
# webcam that only sees your face from straight on won't see the arm bend.
#
# CALIBRATION: before your first set of push-ups each session, you hold a
# T-pose (arms straight out to your sides) for TPOSE_HOLD_SEC. That measures
# what "fully extended arm" actually reads as for your body/camera angle
# (perspective and arm proportions both shift this number around, so a fixed
# guess is less reliable than measuring it), and the "up"/"down" thresholds
# used while counting reps are derived from that instead of a fixed guess.

import math
from dataclasses import dataclass
from pathlib import Path

from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import PoseLandmarker, PoseLandmarkerOptions, RunningMode

# ---- tunable: fallback thresholds, used only if a set is somehow started
# before calibration has run ----
PUSHUP_DOWN_ANGLE_DEG = 100.0  # elbow angle below this = "down" (chest near the ground)
PUSHUP_UP_ANGLE_DEG = 150.0    # elbow angle above this = "up" (arms extended) -- a rep completes here

# ---- tunable: calibration ----
TPOSE_HOLD_SEC = 2.0             # how long the T-pose must be held to calibrate
TPOSE_MIN_ANGLE_DEG = 150.0       # both elbows must be at least this straight to count as "T-pose"
TPOSE_HEIGHT_TOLERANCE = 0.15    # wrist must be within this many meters of shoulder height (arms roughly horizontal, not hanging down)
CALIBRATION_BUFFER_DEG = 15.0    # "up" threshold sits this many degrees below your measured straight-arm reading
PUSHUP_HYSTERESIS_GAP_DEG = 50.0  # "down" threshold sits this many degrees below the calibrated "up" threshold

LEFT_ARM = (11, 13, 15)   # shoulder, elbow, wrist landmark indices
RIGHT_ARM = (12, 14, 16)


def _angle_deg(a, b, c):
    """Angle at point b, between rays b->a and b->c (3D)."""
    v1 = (a.x - b.x, a.y - b.y, a.z - b.z)
    v2 = (c.x - b.x, c.y - b.y, c.z - b.z)
    dot = sum(p * q for p, q in zip(v1, v2))
    n1 = math.sqrt(sum(p * p for p in v1))
    n2 = math.sqrt(sum(p * p for p in v2))
    cos_angle = max(-1.0, min(1.0, dot / (n1 * n2 + 1e-9)))
    return math.degrees(math.acos(cos_angle))


@dataclass
class CalibrationStatus:
    visible: bool
    angle: float = None       # current measured (average) elbow angle, if visible
    holding: bool = False     # is a valid T-pose being held right now
    progress: float = 0.0     # 0.0-1.0 toward TPOSE_HOLD_SEC
    done: bool = False        # calibration just completed this frame


class PushupCounter:
    """Usage:
        with PushupCounter(PUSHUP_MODEL_PATH) as counter:
            # once per session, before the first set:
            status = counter.update_calibration(mp_image, timestamp_ms, time.time())
            # ... once status.done, calibration is applied automatically ...

            counter.reset()
            visible, elbow_angle = counter.update(mp_image, timestamp_ms)
            print(counter.reps)
    """

    def __init__(self, model_path):
        self.model_path = Path(model_path)
        self._landmarker = None
        self.reps = 0
        self.phase = "up"
        self.up_angle = PUSHUP_UP_ANGLE_DEG      # replaced once calibrate() completes
        self.down_angle = PUSHUP_DOWN_ANGLE_DEG
        self.calibrated = False
        self._calib_hold_since = None

    def __enter__(self):
        options = PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(self.model_path)),
            running_mode=RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=0.5,
            min_pose_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self._landmarker = PoseLandmarker.create_from_options(options)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._landmarker is not None:
            self._landmarker.close()
            self._landmarker = None

    def reset(self):
        self.reps = 0
        self.phase = "up"

    def get_elbow_angle(self, mp_image, timestamp_ms):
        """Runs the landmarker once. Returns (visible, avg_elbow_angle_deg, landmarks_or_None)."""
        result = self._landmarker.detect_for_video(mp_image, timestamp_ms)
        if not result.pose_world_landmarks:
            return False, None, None
        lm = result.pose_world_landmarks[0]
        left_angle = _angle_deg(lm[LEFT_ARM[0]], lm[LEFT_ARM[1]], lm[LEFT_ARM[2]])
        right_angle = _angle_deg(lm[RIGHT_ARM[0]], lm[RIGHT_ARM[1]], lm[RIGHT_ARM[2]])
        return True, (left_angle + right_angle) / 2, lm

    def update_calibration(self, mp_image, timestamp_ms, now):
        """Call once per frame while showing the T-pose calibration screen."""
        visible, avg_angle, lm = self.get_elbow_angle(mp_image, timestamp_ms)
        if not visible:
            self._calib_hold_since = None
            return CalibrationStatus(visible=False)

        left_angle = _angle_deg(lm[LEFT_ARM[0]], lm[LEFT_ARM[1]], lm[LEFT_ARM[2]])
        right_angle = _angle_deg(lm[RIGHT_ARM[0]], lm[RIGHT_ARM[1]], lm[RIGHT_ARM[2]])
        left_level = abs(lm[LEFT_ARM[2]].y - lm[LEFT_ARM[0]].y) < TPOSE_HEIGHT_TOLERANCE
        right_level = abs(lm[RIGHT_ARM[2]].y - lm[RIGHT_ARM[0]].y) < TPOSE_HEIGHT_TOLERANCE

        is_tpose = (left_angle >= TPOSE_MIN_ANGLE_DEG and right_angle >= TPOSE_MIN_ANGLE_DEG
                    and left_level and right_level)

        if not is_tpose:
            self._calib_hold_since = None
            return CalibrationStatus(visible=True, angle=avg_angle, holding=False, progress=0.0)

        if self._calib_hold_since is None:
            self._calib_hold_since = now
        held = now - self._calib_hold_since
        progress = min(1.0, held / TPOSE_HOLD_SEC)

        if held >= TPOSE_HOLD_SEC:
            self.up_angle = avg_angle - CALIBRATION_BUFFER_DEG
            self.down_angle = self.up_angle - PUSHUP_HYSTERESIS_GAP_DEG
            self.calibrated = True
            return CalibrationStatus(visible=True, angle=avg_angle, holding=True, progress=1.0, done=True)

        return CalibrationStatus(visible=True, angle=avg_angle, holding=True, progress=progress)

    def update(self, mp_image, timestamp_ms):
        """Call once per frame while counting reps. Returns (visible, elbow_angle_deg)."""
        visible, elbow_angle, _ = self.get_elbow_angle(mp_image, timestamp_ms)
        if not visible:
            return False, None

        if self.phase == "up" and elbow_angle < self.down_angle:
            self.phase = "down"
        elif self.phase == "down" and elbow_angle > self.up_angle:
            self.phase = "up"
            self.reps += 1

        return True, elbow_angle
