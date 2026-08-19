# Challenge side of Study Warden. Body-pose matching (PoseLandmarker) has
# been dropped -- it needs a clear full-body photo, which reaction
# images/gifs/memes/cartoons don't give it. Matching is now driven by
# gesture_config.py: each pose file can require a face expression (either raw
# blendshape thresholds or a named expression from expression_classifier.py),
# an eye-gaze direction (via eye_tracker.py), and/or a hand gesture (checked
# via HandLandmarker, still used here).
#
# Needs, next to this file:
#   vision/hand_landmarker.task
#     https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task
#
# GIF support needs Pillow:  pip install pillow

import math
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import HandLandmarker, HandLandmarkerOptions, RunningMode

from gesture_config import GESTURES, DEFAULT_GESTURE
from expression_classifier import classify_expression
from eye_tracker import get_gaze_direction

try:
    from PIL import Image, ImageSequence
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

# ---- tunable ----
POSE_MATCH_THRESHOLD_PCT = 70.0  # combined face/hand score must reach this % to count as matched

# distance thresholds below are in normalized (0-1) frame coordinates
NEAR_MOUTH_CLOSE, NEAR_MOUTH_FAR = 0.05, 0.35
HANDS_TOGETHER_CLOSE, HANDS_TOGETHER_FAR = 0.0, 0.25
HANDS_ON_FACE_CLOSE, HANDS_ON_FACE_FAR = 0.0, 0.30

# An animated (.gif) reference pose demonstrates a MOVEMENT, not just a
# static shape -- holding still and merely matching the gesture's end
# position shouldn't be enough to pass one. MOTION_WINDOW_SEC is how far
# back "recent movement" looks; MOTION_MIN_PATH is the total normalized-
# coordinate path length the tracked point (hand or head, whichever the
# pose's own spec cares about -- see PoseChallenge._motion_track_point())
# must cover within that window to count as "actually moving." Static
# (single-frame) poses are completely unaffected -- see is_gif_challenge().
MOTION_WINDOW_SEC = 1.2
MOTION_MIN_PATH = 0.05

SUPPORTED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif")


@dataclass
class MatchResult:
    visible: bool                        # was a face or hand detected at all this frame?
    match_pct: float = 0.0
    breakdown: dict = field(default_factory=dict)   # e.g. {"face": 82.0} or {"hand": 40.0}
    hand_points_norm: list = field(default_factory=list)  # list of hands, each 21 (x, y) normalized points
    motion_ok: bool = True   # always True for a static pose; for a gif, whether enough real movement was seen


@dataclass
class PoseEntry:
    name: str
    frames: list     # list of BGR numpy arrays (1 for a static image, many for a gif)
    durations: list  # ms to show each frame for
    spec: dict       # gesture requirement, e.g. {"face": {...}} and/or {"hand": "near_mouth"}


def resize_keep_aspect(img, max_dim=500):
    h, w = img.shape[:2]
    scale = max_dim / max(h, w)
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
    return img


def load_image_frames(path, max_dim=500):
    """Return (frames, durations_ms). A static image comes back as a single
    frame; a .gif comes back as all of its frames, correctly composited
    (uses PIL's ImageSequence.Iterator, not raw seek(), to avoid the classic
    "GIF frames look corrupted" bug some naive extractors hit)."""
    path = Path(path)
    if path.suffix.lower() == ".gif":
        if not _HAS_PIL:
            raise RuntimeError("Reading .gif pose files needs Pillow: pip install pillow")
        pil_img = Image.open(path)
        frames, durations = [], []
        for frame in ImageSequence.Iterator(pil_img):
            import numpy as np  # local import: only needed for the gif path
            rgb = np.array(frame.convert("RGB"))
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            frames.append(resize_keep_aspect(bgr, max_dim))
            durations.append(frame.info.get("duration", 100) or 100)
        return frames, durations

    img = cv2.imread(str(path))
    if img is None:
        return [], []
    return [resize_keep_aspect(img, max_dim)], [1000]


# ---- hand-gesture scoring primitives ----
# Each takes normalized (0-1) points and returns a 0.0-1.0 "how close is this
# to the target gesture" score, not just True/False, so the on-screen readout
# can show a percentage instead of a flat pass/fail.

def _hand_centroid(hand):
    xs = [p[0] for p in hand]
    ys = [p[1] for p in hand]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _closeness(d, close, far):
    """1.0 at/under `close`, 0.0 at/over `far`, linear between."""
    if d <= close:
        return 1.0
    if d >= far:
        return 0.0
    return 1.0 - (d - close) / (far - close)


def score_near_mouth(hands_norm, mouth_point):
    if not hands_norm or mouth_point is None:
        return 0.0
    best = min(_dist(_hand_centroid(h), mouth_point) for h in hands_norm)
    return _closeness(best, NEAR_MOUTH_CLOSE, NEAR_MOUTH_FAR)


def score_hands_together(hands_norm):
    if len(hands_norm) < 2:
        return 0.0
    d = _dist(_hand_centroid(hands_norm[0]), _hand_centroid(hands_norm[1]))
    return _closeness(d, HANDS_TOGETHER_CLOSE, HANDS_TOGETHER_FAR)


def score_hands_on_face(hands_norm, face_point):
    if not hands_norm or face_point is None:
        return 0.0
    scores = [_closeness(_dist(_hand_centroid(h), face_point), HANDS_ON_FACE_CLOSE, HANDS_ON_FACE_FAR)
              for h in hands_norm]
    if len(scores) >= 2:
        return sum(sorted(scores)[-2:]) / 2  # reward both hands, but don't require exactly 2
    return scores[0] * 0.6  # one hand near the face = partial credit


def score_any_hand_visible(hands_norm):
    return 1.0 if hands_norm else 0.0


_HAND_SCORERS = {
    "near_mouth": lambda hands, mouth, face: score_near_mouth(hands, mouth),
    "hands_together": lambda hands, mouth, face: score_hands_together(hands),
    "hands_on_face": lambda hands, mouth, face: score_hands_on_face(hands, face),
    "any_hand_visible": lambda hands, mouth, face: score_any_hand_visible(hands),
}


def score_face(blendshape_scores, requirements):
    if not requirements:
        return 1.0
    blendshape_scores = blendshape_scores or {}
    if isinstance(requirements, str):
        # a named expression from expression_classifier.py, e.g. "Happy"
        label, confidence = classify_expression(blendshape_scores)
        return confidence if label == requirements else 0.0
    parts = []
    for name, threshold in requirements.items():
        actual = blendshape_scores.get(name, 0.0)
        parts.append(min(1.0, actual / threshold) if threshold > 0 else 1.0)
    return sum(parts) / len(parts)


def score_gaze(face_landmarks, required_direction):
    if not face_landmarks or not required_direction:
        return 0.0
    direction, _, _ = get_gaze_direction(face_landmarks)
    return 1.0 if direction == required_direction else 0.0


class PoseChallenge:
    """Owns the reference-pose bank (loaded from gesture_config.GESTURES) plus
    the live hand landmarker.

    Usage:
        challenge = PoseChallenge(HAND_MODEL_PATH, POSES_FOLDER).load_bank()
        with challenge:
            img = challenge.pick_random_challenge()
            ...
            result = challenge.check_match(mp_image, timestamp_ms,
                                            face_blendshapes=scores,
                                            mouth_point=mouth_pt, face_center_point=nose_pt)
            if challenge.is_match(result):
                ...
            display_frame = challenge.get_display_frame()  # call every loop iteration to animate gifs
    """

    def __init__(self, hand_model_path, poses_folder):
        self.hand_model_path = Path(hand_model_path)
        self.poses_folder = Path(poses_folder)
        self.bank = []       # list of PoseEntry
        self.current = None  # PoseEntry currently being challenged
        self._anim_start = 0.0
        self._hand_landmarker = None
        self._motion_history = []  # [(timestamp, (x, y)), ...] for the active gif challenge's tracked point

    def load_bank(self):
        if not self.poses_folder.exists():
            raise FileNotFoundError(
                f"Pose folder not found at {self.poses_folder}\n"
                "Create it and add a few reference images or gifs (.jpg/.jpeg/.png/.gif)."
            )
        paths = sorted(p for p in self.poses_folder.iterdir() if p.suffix.lower() in SUPPORTED_EXTENSIONS)
        if not paths:
            raise FileNotFoundError(f"No {SUPPORTED_EXTENSIONS} files found in {self.poses_folder}")

        for path in paths:
            frames, durations = load_image_frames(path)
            if not frames:
                print(f"Could not read {path.name}, skipping.")
                continue
            spec = GESTURES.get(path.name, DEFAULT_GESTURE)
            if path.name not in GESTURES:
                print(f"{path.name}: no entry in gesture_config.py, using default {DEFAULT_GESTURE}")
            else:
                print(f"{path.name}: requires {spec}")
            self.bank.append(PoseEntry(path.name, frames, durations, spec))

        if not self.bank:
            raise RuntimeError(f"None of the files in {self.poses_folder} could be loaded.")
        print(f"Loaded {len(self.bank)} reference pose(s) from {self.poses_folder}")
        return self

    def __enter__(self):
        hand_options = HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(self.hand_model_path)),
            running_mode=RunningMode.VIDEO,
            num_hands=2,
            min_hand_detection_confidence=0.5,
            min_hand_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self._hand_landmarker = HandLandmarker.create_from_options(hand_options)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._hand_landmarker is not None:
            self._hand_landmarker.close()
            self._hand_landmarker = None

    def pick_random_challenge(self):
        """Choose a new random reference pose/gif and return its first display frame."""
        self.current = random.choice(self.bank)
        self._anim_start = time.time()
        self._motion_history = []  # don't carry stale movement from whatever the PREVIOUS challenge was
        return self.get_display_frame()

    def is_gif_challenge(self):
        """Whether the active challenge is an animated (.gif) reference --
        multi-frame is exactly what load_image_frames() gives a real gif,
        a static image always comes back as a single frame."""
        return self.current is not None and len(self.current.frames) > 1

    def _motion_track_point(self, hands_norm, face_center_point):
        """The single (x, y) point whose movement counts toward a gif
        challenge's "are you actually moving" requirement -- the hand
        centroid if the pose's own spec cares about hand position
        (a moving-hands gesture), otherwise head position (nose) as a
        proxy for body/head movement (e.g. a nod/shake demonstrated in
        the gif). Ties the motion check to whatever that specific gesture
        is actually about, rather than one fixed body part for every gif."""
        spec = self.current.spec
        if "hand" in spec and hands_norm:
            return _hand_centroid(hands_norm[0])
        return face_center_point

    def _track_motion(self, point):
        """Records `point` for the active challenge and returns the total
        path length traveled within the last MOTION_WINDOW_SEC -- a
        motionless hold (webcam noise/hand tremor aside) stays near zero;
        real movement accumulates real distance."""
        now = time.time()
        if point is not None:
            self._motion_history.append((now, point))
        cutoff = now - MOTION_WINDOW_SEC
        self._motion_history = [(t, p) for t, p in self._motion_history if t >= cutoff]
        return sum(
            _dist(a, b) for (_, a), (_, b) in zip(self._motion_history, self._motion_history[1:])
        )

    def get_display_frame(self):
        """Current frame to show for the active challenge -- call this every
        loop iteration so gifs animate; it's cheap when there's only 1 frame."""
        entry = self.current
        if len(entry.frames) == 1:
            return entry.frames[0]
        total = sum(entry.durations)
        elapsed_ms = ((time.time() - self._anim_start) * 1000) % total
        acc = 0
        for frame, dur in zip(entry.frames, entry.durations):
            acc += dur
            if elapsed_ms < acc:
                return frame
        return entry.frames[-1]

    def check_match(self, mp_image, timestamp_ms, face_blendshapes=None, mouth_point=None,
                     face_center_point=None, face_landmarks=None):
        """Run hand detection on one live frame and score it against the
        current challenge's required face/gaze/hand gesture."""
        hand_result = self._hand_landmarker.detect_for_video(mp_image, timestamp_ms)
        hands_norm = [[(lm.x, lm.y) for lm in hand] for hand in hand_result.hand_landmarks]

        spec = self.current.spec
        breakdown = {}
        if "face" in spec:
            breakdown["face"] = round(score_face(face_blendshapes, spec["face"]) * 100, 1)
        if "gaze" in spec:
            breakdown["gaze"] = round(score_gaze(face_landmarks, spec["gaze"]) * 100, 1)
        if "hand" in spec:
            scorer = _HAND_SCORERS.get(spec["hand"])
            hand_score = scorer(hands_norm, mouth_point, face_center_point) if scorer else 0.0
            breakdown["hand"] = round(hand_score * 100, 1)

        visible = bool(face_blendshapes) or bool(hands_norm) or bool(face_landmarks)
        match_pct = sum(breakdown.values()) / len(breakdown) if breakdown else 0.0

        # A gif demonstrates a MOVEMENT -- holding still and merely landing
        # on the gesture's end position shouldn't be enough to pass one.
        # "motion" is tracked separately from match_pct (not averaged in)
        # so a strong static score can't paper over standing still; it's
        # still surfaced in breakdown so the on-screen readout shows
        # progress toward it either way.
        motion_ok = True
        if self.is_gif_challenge():
            motion_point = self._motion_track_point(hands_norm, face_center_point)
            path_length = self._track_motion(motion_point)
            motion_ok = path_length >= MOTION_MIN_PATH
            breakdown["motion"] = round(min(path_length / MOTION_MIN_PATH, 1.0) * 100, 1)

        return MatchResult(
            visible=visible, match_pct=match_pct, breakdown=breakdown,
            hand_points_norm=hands_norm, motion_ok=motion_ok,
        )

    def is_match(self, result: MatchResult) -> bool:
        return result.visible and result.match_pct >= POSE_MATCH_THRESHOLD_PCT and result.motion_ok
