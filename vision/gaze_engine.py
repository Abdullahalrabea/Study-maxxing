# OpenCV / MediaPipe for tracking facial expressions, eye blinks, and head
# pose. This is the entry point (`py vision/gaze_engine.py`) -- it also drives
# the distraction-challenge state machine, delegating all gesture-matching
# work to pose_engine.PoseChallenge (face blendshapes + HandLandmarker; see
# gesture_config.py for what each reference pose requires).
#
# `mediapipe.solutions` (the module the original ModuleNotFoundError pointed
# at) has been fully removed from current mediapipe releases. This uses its
# replacement, the `mediapipe.tasks` API.
#
# ONE-TIME SETUP:
#
# 1) Download FOUR model bundles and save them next to this script:
#      Face   (~4 MB): https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
#      Hand   (~8 MB): https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task
#      Phone  (~4 MB): https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/int8/1/efficientdet_lite0.tflite
#      Pose   (~6 MB): https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task
#      (the last one is back -- it's used again, but now only for counting push-up reps, not pose matching)
#
#    PowerShell:
#      Invoke-WebRequest -Uri "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task" -OutFile "vision\face_landmarker.task"
#      Invoke-WebRequest -Uri "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task" -OutFile "vision\hand_landmarker.task"
#      Invoke-WebRequest -Uri "https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/int8/1/efficientdet_lite0.tflite" -OutFile "vision\efficientdet_lite0.tflite"
#      Invoke-WebRequest -Uri "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task" -OutFile "vision\pose_landmarker_lite.task"
#
# 2) Create a "poses" folder next to this script and drop reference images or
#    gifs in it (.jpg/.jpeg/.png/.gif), then describe what each one requires
#    in gesture_config.py (face expression and/or hand gesture).
#
# 3) pip install pillow   (only needed for reading .gif pose files)
#
# 4) Keep pose_engine.py, gesture_config.py, phone_detector.py, and
#    pushup_counter.py in the same folder as this script -- all are imported
#    below.
#
# RULES: hold your phone up for PHONE_HOLD_TIMEOUT_SEC seconds -> pose
# challenge, same as looking away. Every PHONE_VIOLATIONS_BEFORE_PUSHUPS-th
# time that happens, you also have to do PUSHUP_COUNT push-ups afterward
# (see the tunable constants below -- PUSHUP_COUNT is the one to change if
# you want a different number of push-ups).

import time
from pathlib import Path

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import (
    FaceLandmarker,
    FaceLandmarkerOptions,
    RunningMode,
    FaceLandmarksConnections,
    HandLandmarksConnections,
)
from mediapipe.tasks.python.vision import drawing_utils as mp_drawing
from mediapipe.tasks.python.vision import drawing_styles as mp_drawing_styles

from pose_engine import PoseChallenge
from phone_detector import PhoneDetector, PHONE_LIKE_LABELS, PHONE_SCORE_THRESHOLD
from pushup_counter import PushupCounter, TPOSE_HOLD_SEC
from eye_tracker import get_gaze_direction
from expression_classifier import classify_expression

SCRIPT_DIR = Path(__file__).parent
MODEL_PATH = SCRIPT_DIR / "face_landmarker.task"
HAND_MODEL_PATH = SCRIPT_DIR / "hand_landmarker.task"
PHONE_MODEL_PATH = SCRIPT_DIR / "efficientdet_lite0.tflite"
PUSHUP_MODEL_PATH = SCRIPT_DIR / "pose_landmarker_lite.task"
POSES_FOLDER = SCRIPT_DIR / "poses"

# landmark indices used as face-relative reference points for hand gestures
MOUTH_LANDMARK_IDX = 13   # upper inner lip
NOSE_LANDMARK_IDX = 1     # nose tip, used as a general "face center"

# ---- tunable thresholds ----
BLINK_SCORE_THRESHOLD = 0.5     # eyeBlinkLeft/Right blendshape score above this = eye closed
YAWN_SCORE_THRESHOLD = 0.5      # jawOpen blendshape score above this = mouth open
YAW_LOOK_AWAY_DEG = 15.0        # head turned left/right past this = "looking away"
PITCH_LOOK_AWAY_DEG = 12.0      # head tilted up/down past this = "looking away"

LOOK_AWAY_TIMEOUT_SEC = 5.0     # not centered for longer than this -> pose challenge
POSE_HOLD_SEC = 1.5             # how long a matched pose must be held to count as "done"
RECENTER_HOLD_SEC = 1.0         # how long you must hold "Center" to fully resume

PHONE_HOLD_TIMEOUT_SEC = 5.0            # phone visible for longer than this -> pose challenge
PHONE_VIOLATIONS_BEFORE_PUSHUPS = 3      # every Nth phone violation also requires push-ups
PUSHUP_COUNT = 10                        # <-- CHANGE THIS to require a different number of push-ups

POSE_WINDOW = "Do this pose!"
HAND_POINT_COLOR = (255, 180, 0)


def rotation_matrix_to_euler_deg(rotation):
    """3x3 rotation matrix -> (pitch, yaw, roll) in degrees.
    pitch = rotation about X (nodding up/down)
    yaw   = rotation about Y (turning left/right)
    roll  = rotation about Z (tilting sideways)
    """
    sy = np.sqrt(rotation[0, 0] ** 2 + rotation[1, 0] ** 2)
    if sy > 1e-6:
        pitch = np.arctan2(rotation[2, 1], rotation[2, 2])
        yaw = np.arctan2(-rotation[2, 0], sy)
        roll = np.arctan2(rotation[1, 0], rotation[0, 0])
    else:
        pitch = np.arctan2(-rotation[1, 2], rotation[1, 1])
        yaw = np.arctan2(-rotation[2, 0], sy)
        roll = 0.0
    return np.degrees(pitch), np.degrees(yaw), np.degrees(roll)


def draw_hand_points(frame, hands_norm, color=HAND_POINT_COLOR):
    """Circles + connecting lines on each detected hand's 21 landmarks."""
    if not hands_norm:
        return
    h, w = frame.shape[:2]
    for hand in hands_norm:
        px = [(int(x * w), int(y * h)) for x, y in hand]
        for conn in HandLandmarksConnections.HAND_CONNECTIONS:
            cv2.line(frame, px[conn.start], px[conn.end], color, 1)
        for pt in px:
            cv2.circle(frame, pt, 3, color, -1)


def draw_detections(frame, detections, phone_like_labels, phone_threshold):
    """Boxes + label + score for every object the phone detector saw this
    frame, color-coded by whether it counts as a phone hit. This is the main
    debugging tool if phone detection feels unreliable -- watch what label
    and score your phone actually gets."""
    for d in detections:
        x, y0, w, h = d.bbox
        is_hit = d.label in phone_like_labels and d.score >= phone_threshold
        color = (0, 255, 0) if is_hit else (130, 130, 130)
        cv2.rectangle(frame, (x, y0), (x + w, y0 + h), color, 2)
        cv2.putText(frame, f"{d.label} {d.score:.2f}", (x, max(0, y0 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)


DISRUPTIVE_STATES = {"CHALLENGE", "CALIBRATE", "PUSHUPS", "RECENTER"}  # states where the user must stop studying


def main(should_stop=None, on_disruption_change=None, on_ready=None, is_suppressed=None,
         on_reset_requested=None, on_frame=None, on_penalty_pending_change=None, initial_pushups_owed=0):
    """should_stop: optional zero-arg callable, checked once per frame; once
    it returns True the loop exits cleanly (camera released, windows closed)
    -- lets external code (e.g. a study timer finishing) close this down
    without needing the 'q' keypress.
    on_disruption_change: optional callable(is_disruptive: bool), called
    only when crossing into/out of a pose-challenge/calibration/push-ups/
    recentering state -- lets external code (e.g. a study timer) pause
    while the user's doing the challenge, staying paused through the
    recentering step too, and resume only once they're actually back to
    MONITORING (not the moment the pose itself is matched).
    on_ready: optional zero-arg callable, called exactly once, right after
    the webcam has delivered its first real frame and every model is loaded
    -- lets external code (e.g. a study timer) wait to start a countdown
    until monitoring is actually active, rather than racing it.
    is_suppressed: optional zero-arg callable, checked once per frame while
    MONITORING; while it returns True, distraction detection (look-away/
    phone timers) is paused rather than accumulating -- e.g. during a break,
    where being away from the desk shouldn't count against you.
    on_reset_requested: optional zero-arg callable, checked once per frame;
    should return True at most once per reset (a one-shot "consume" check).
    When it does, all distraction counters/streaks are cleared -- e.g. so
    the second study block starts with a clean slate instead of carrying
    over a phone-violation count or an in-progress look-away streak from
    before the break.
    on_frame: optional callable(main_frame_bgr, pose_frame_bgr_or_None),
    called once per processed frame with the annotated webcam frame (BGR
    numpy array) and -- only while a pose challenge is actively being shown
    -- the reference pose image to display alongside it (BGR numpy array),
    or None otherwise. When given, this REPLACES the native OpenCV windows
    entirely (for embedding the feed in a Qt widget instead of a separate
    OS window) -- the 'q' keypress no longer quits either, since there's no
    OpenCV window left to receive it; use should_stop for that. When not
    given, falls back to the original cv2.imshow windows (e.g. for
    `python vision/gaze_engine.py` standalone runs).
    on_penalty_pending_change: optional callable(pending: bool, reps_done:
    int, reps_needed: int), called only when crossing into/out of actually
    owing push-ups (state == PUSHUPS, or a violation just triggered one and
    CALIBRATE/CHALLENGE hasn't resolved it yet) -- lets external code
    persist "these are still owed" if the app closes mid-penalty (see
    core/session_snapshot.py's penalty debt).
    initial_pushups_owed: if > 0, the loop starts DIRECTLY in a push-ups-
    owed state (CALIBRATE first if not yet calibrated) instead of
    MONITORING, requiring this many reps -- not PUSHUP_COUNT -- before
    continuing. Used for a standalone "pay off what you owe from last
    time" flow (see main_window.py's PenaltyCompletionDialog) that isn't a
    real study session at all, just settling a persisted penalty debt."""
    for path, label in ((MODEL_PATH, "face"), (HAND_MODEL_PATH, "hand"),
                        (PHONE_MODEL_PATH, "phone"), (PUSHUP_MODEL_PATH, "pushup pose")):
        if not path.exists():
            raise FileNotFoundError(
                f"{label.capitalize()} model bundle not found at {path}\n"
                "See the setup instructions in the comment at the top of this file."
            )

    pose_challenge = PoseChallenge(HAND_MODEL_PATH, POSES_FOLDER).load_bank()
    phone_detector = PhoneDetector(PHONE_MODEL_PATH)
    pushup_counter = PushupCounter(PUSHUP_MODEL_PATH)

    face_options = FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(MODEL_PATH)),
        running_mode=RunningMode.VIDEO,
        num_faces=1,
        min_face_detection_confidence=0.5,
        min_face_presence_confidence=0.5,
        min_tracking_confidence=0.5,
        output_face_blendshapes=True,
        output_facial_transformation_matrixes=True,
    )

    camera = cv2.VideoCapture(0)
    start_time = time.time()

    # state machine: MONITORING -> CHALLENGE -> (PUSHUPS ->) RECENTER -> MONITORING
    # initial_pushups_owed skips straight past MONITORING/CHALLENGE (there's
    # nothing to challenge -- the penalty was already triggered last time,
    # this run exists purely to pay it off) into PUSHUPS/CALIBRATE directly.
    state = "MONITORING" if initial_pushups_owed <= 0 else (
        "PUSHUPS" if pushup_counter.calibrated else "CALIBRATE"
    )
    away_since = None
    phone_since = None
    match_since = None
    recenter_since = None
    phone_violation_count = 0
    pushups_pending = False
    pushups_required = initial_pushups_owed if initial_pushups_owed > 0 else PUSHUP_COUNT
    last_disruptive = False
    last_penalty_pending = False
    ready_reported = False
    if state == "PUSHUPS":
        pushup_counter.reset()

    with FaceLandmarker.create_from_options(face_options) as face_landmarker, \
         pose_challenge, phone_detector, pushup_counter:
        print("Vision Engine Active. Face your camera! Press 'q' to quit.")

        while True:
            success, frame = camera.read()
            if not success:
                continue

            if not ready_reported:
                ready_reported = True
                if on_ready is not None:
                    on_ready()

            if on_reset_requested is not None and on_reset_requested():
                away_since = None
                phone_since = None
                phone_violation_count = 0
                pushups_pending = False

            frame = cv2.flip(frame, 1)
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
            timestamp_ms = int((time.time() - start_time) * 1000)
            now = time.time()
            pose_frame = None  # set below only while a pose challenge is actively being shown

            is_disruptive = state in DISRUPTIVE_STATES
            if is_disruptive != last_disruptive:
                last_disruptive = is_disruptive
                if on_disruption_change is not None:
                    on_disruption_change(is_disruptive)

            # A penalty is "pending" (owed, not yet paid off) from the
            # moment a violation triggers it (pushups_pending, still in
            # CHALLENGE/CALIBRATE) through the entire PUSHUPS state itself
            # -- only clears once reps actually complete and state moves
            # on to RECENTER. Fires every frame WHILE pending (not just on
            # the transition) so reps_done stays live -- if the app closes
            # or lockdown exits mid-PUSHUPS, the caller (main_window.py)
            # needs an up-to-date rep count at THAT moment, not whatever
            # it was back when the penalty first started.
            penalty_pending = pushups_pending or state == "PUSHUPS"
            if penalty_pending or penalty_pending != last_penalty_pending:
                if on_penalty_pending_change is not None:
                    on_penalty_pending_change(penalty_pending, pushup_counter.reps, pushups_required)
            last_penalty_pending = penalty_pending

            face_result = face_landmarker.detect_for_video(mp_image, timestamp_ms)

            y = 30
            direction = None       # None = no face detected right now
            gaze_direction = None  # None = no face detected right now
            blendshape_scores = {}
            mouth_point = None
            face_center_point = None

            if face_result.face_landmarks:
                landmarks = face_result.face_landmarks[0]
                mouth_point = (landmarks[MOUTH_LANDMARK_IDX].x, landmarks[MOUTH_LANDMARK_IDX].y)
                face_center_point = (landmarks[NOSE_LANDMARK_IDX].x, landmarks[NOSE_LANDMARK_IDX].y)

                mp_drawing.draw_landmarks(
                    image=frame,
                    landmark_list=landmarks,
                    connections=FaceLandmarksConnections.FACE_LANDMARKS_CONTOURS,
                    landmark_drawing_spec=mp_drawing.DrawingSpec(color=(0, 255, 0), thickness=1, circle_radius=1),
                    connection_drawing_spec=mp_drawing.DrawingSpec(color=(0, 255, 0), thickness=1),
                )

                if face_result.face_blendshapes:
                    blendshape_scores = {c.category_name: c.score for c in face_result.face_blendshapes[0]}

                    eyes_closed = (blendshape_scores.get("eyeBlinkLeft", 0.0) > BLINK_SCORE_THRESHOLD and
                                   blendshape_scores.get("eyeBlinkRight", 0.0) > BLINK_SCORE_THRESHOLD)
                    mouth_open = blendshape_scores.get("jawOpen", 0.0) > YAWN_SCORE_THRESHOLD

                    if eyes_closed:
                        cv2.putText(frame, "Blinking", (10, y),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                        y += 30
                    if mouth_open:
                        cv2.putText(frame, "Mouth open / yawning", (10, y),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 165, 255), 2)
                        y += 30

                    expression, expression_conf = classify_expression(blendshape_scores)
                    if expression != "Neutral":
                        cv2.putText(frame, f"Expression: {expression} ({expression_conf * 100:0.0f}%)",
                                    (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 220, 0), 2)
                        y += 25
                        if expression == "Tired":
                            cv2.putText(frame, "You seem tired -- maybe take a break?", (10, y),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 165, 255), 1)
                            y += 25

                gaze_direction, gaze_h, gaze_v = get_gaze_direction(landmarks)
                cv2.putText(frame, f"Gaze: {gaze_direction} (h={gaze_h:.2f} v={gaze_v:.2f})", (10, y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 200, 100), 2)
                y += 30

                if face_result.facial_transformation_matrixes:
                    rotation = face_result.facial_transformation_matrixes[0][:3, :3]
                    pitch, yaw, roll = rotation_matrix_to_euler_deg(rotation)

                    direction = "Center"
                    if yaw > YAW_LOOK_AWAY_DEG:
                        direction = "Looking Right"
                    elif yaw < -YAW_LOOK_AWAY_DEG:
                        direction = "Looking Left"
                    elif pitch > PITCH_LOOK_AWAY_DEG:
                        direction = "Looking Down"
                    elif pitch < -PITCH_LOOK_AWAY_DEG:
                        direction = "Looking Up"

                    cv2.putText(frame, f"Head: {direction}", (10, y),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)
                    y += 25
                    cv2.putText(frame, f"pitch {pitch:+.1f}  yaw {yaw:+.1f}  roll {roll:+.1f}",
                                (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 0), 1)
                    y += 30
            else:
                cv2.putText(frame, "No face detected", (10, y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
                y += 30

            # Only left/right counts as "not centered" for violation purposes
            # -- looking down (notes/keyboard) or up doesn't trigger anything.
            # This now considers both head turn AND eye gaze: someone can glance
            # sideways at a phone without turning their head, which head-pose
            # alone wouldn't catch. No face detected still counts (same as
            # before), since we can't tell orientation at all in that case.
            head_turned_away = direction is None or direction in ("Looking Left", "Looking Right")
            eyes_turned_away = gaze_direction in ("Left", "Right")
            turned_away = head_turned_away or eyes_turned_away

            # ---- distraction-challenge state machine ----
            if state == "MONITORING":
                if is_suppressed is not None and is_suppressed():
                    # e.g. on a break -- don't accumulate or act on distraction
                    # signals, and don't carry over a stale streak once it ends
                    away_since = None
                    phone_since = None
                    cv2.putText(frame, "On break -- not monitoring for distractions", (10, y),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 0), 2)
                    y += 25
                else:
                    detections = phone_detector.get_detections(mp_image, timestamp_ms)
                    draw_detections(frame, detections, PHONE_LIKE_LABELS, PHONE_SCORE_THRESHOLD)
                    phone_visible = phone_detector.is_phone_visible(detections)

                    if turned_away:
                        if away_since is None:
                            away_since = now
                    else:
                        away_since = None

                    if phone_visible:
                        if phone_since is None:
                            phone_since = now
                    else:
                        phone_since = None

                    away_elapsed = (now - away_since) if away_since else 0.0
                    phone_elapsed = (now - phone_since) if phone_since else 0.0

                    if away_since:
                        cv2.putText(frame, f"Not centered: {away_elapsed:0.1f}s", (10, y),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 140, 255), 2)
                        y += 25
                    if phone_since:
                        cv2.putText(frame, f"Phone visible: {phone_elapsed:0.1f}s", (10, y),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 140, 255), 2)
                        y += 25

                    if away_elapsed > LOOK_AWAY_TIMEOUT_SEC:
                        pose_challenge.pick_random_challenge()
                        state = "CHALLENGE"
                        match_since = None
                        away_since = None
                        phone_since = None
                    elif phone_elapsed > PHONE_HOLD_TIMEOUT_SEC:
                        phone_violation_count += 1
                        pushups_pending = (phone_violation_count % PHONE_VIOLATIONS_BEFORE_PUSHUPS == 0)
                        if pushups_pending:
                            pushups_required = PUSHUP_COUNT  # a fresh violation always owes the STANDARD
                                                              # count, even if this run started by paying off
                                                              # a differently-sized owed debt from last time
                        pose_challenge.pick_random_challenge()
                        state = "CHALLENGE"
                        match_since = None
                        away_since = None
                        phone_since = None

            elif state == "CHALLENGE":
                pose_frame = pose_challenge.get_display_frame()
                if on_frame is None:
                    cv2.imshow(POSE_WINDOW, pose_frame)

                cv2.putText(frame, f"Match the pose: {pose_challenge.current.name}", (10, y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2)
                y += 30

                result = pose_challenge.check_match(
                    mp_image, timestamp_ms,
                    face_blendshapes=blendshape_scores,
                    mouth_point=mouth_point,
                    face_center_point=face_center_point,
                    face_landmarks=landmarks if face_result.face_landmarks else None,
                )
                draw_hand_points(frame, result.hand_points_norm)

                if result.visible:
                    breakdown_text = "  ".join(f"{k}: {v:0.0f}%" for k, v in result.breakdown.items())
                    cv2.putText(frame, breakdown_text, (10, y),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2)
                    y += 30

                    if pose_challenge.is_match(result):
                        if match_since is None:
                            match_since = now
                        held = now - match_since
                        cv2.putText(frame, f"Hold it... {held:0.1f}s", (10, y),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                        y += 30
                        if held >= POSE_HOLD_SEC:
                            if on_frame is None:
                                cv2.destroyWindow(POSE_WINDOW)
                            if pushups_pending:
                                pushups_pending = False
                                if pushup_counter.calibrated:
                                    pushup_counter.reset()
                                    state = "PUSHUPS"
                                else:
                                    state = "CALIBRATE"
                            else:
                                state = "RECENTER"
                                recenter_since = None
                    else:
                        match_since = None
                else:
                    match_since = None
                    cv2.putText(frame, "No face or hands visible", (10, y),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
                    y += 30

            elif state == "CALIBRATE":
                cv2.putText(frame, "Calibration: hold a T-pose (arms straight out to your sides)",
                            (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2)
                y += 30

                calib = pushup_counter.update_calibration(mp_image, timestamp_ms, now)
                if not calib.visible:
                    cv2.putText(frame, "Body not visible - step back so your arms are in frame",
                                (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
                    y += 30
                elif not calib.holding:
                    cv2.putText(frame, f"Straighten your arms out level (elbow angle {calib.angle:0.0f} deg)",
                                (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 140, 255), 2)
                    y += 25
                else:
                    cv2.putText(frame, f"Holding... {calib.progress * TPOSE_HOLD_SEC:0.1f}s / {TPOSE_HOLD_SEC:0.1f}s",
                                (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                    y += 30

                if calib.done:
                    pushup_counter.reset()
                    state = "PUSHUPS"

            elif state == "PUSHUPS":
                if initial_pushups_owed > 0:
                    header = f"Paying off {pushups_required} owed push-ups from last time!"
                else:
                    header = f"Phone violation #{phone_violation_count} -- do {pushups_required} push-ups!"
                cv2.putText(frame, header, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 100, 255), 2)
                y += 30
                cv2.putText(frame, f"Push-ups: {pushup_counter.reps} / {pushups_required}", (10, y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2)
                y += 30

                visible, elbow_angle = pushup_counter.update(mp_image, timestamp_ms)
                if visible:
                    cv2.putText(frame, f"Elbow angle: {elbow_angle:0.0f} deg ({pushup_counter.phase})",
                                (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1)
                    y += 25
                else:
                    cv2.putText(frame, "Body not visible - adjust camera angle", (10, y),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
                    y += 25

                if pushup_counter.reps >= pushups_required:
                    state = "RECENTER"
                    recenter_since = None

            elif state == "RECENTER":
                # Re-checks the phone here too, not just head-centering --
                # without this, finishing a challenge/push-ups while the
                # phone is still sitting in view (e.g. still in your hand)
                # dumps you straight back into MONITORING with a clean
                # slate, which immediately starts a brand new
                # PHONE_HOLD_TIMEOUT_SEC countdown and looks like the
                # countdown "resetting" over and over rather than staying
                # paused for as long as the phone is actually visible.
                detections = phone_detector.get_detections(mp_image, timestamp_ms)
                phone_still_visible = phone_detector.is_phone_visible(detections)
                if phone_still_visible:
                    cv2.putText(frame, "Put your phone away to continue", (10, y),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 140, 255), 2)
                    y += 25
                    recenter_since = None
                elif not turned_away:
                    cv2.putText(frame, "Nice! Now center your face to continue", (10, y),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2)
                    y += 30
                    if recenter_since is None:
                        recenter_since = now
                    if now - recenter_since >= RECENTER_HOLD_SEC:
                        state = "MONITORING"
                        away_since = None
                        phone_since = None
                else:
                    cv2.putText(frame, "Nice! Now center your face to continue", (10, y),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2)
                    y += 30
                    recenter_since = None

            if on_frame is not None:
                on_frame(frame, pose_frame)
                quit_key = False
            else:
                cv2.imshow("Study Warden - Face Mesh Test", frame)
                quit_key = cv2.waitKey(1) & 0xFF == ord('q')

            if quit_key or (should_stop is not None and should_stop()):
                break

    camera.release()
    if on_frame is None:
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
