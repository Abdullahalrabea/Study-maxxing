# Eye-gaze direction estimator for Study Warden. Uses the iris landmarks
# MediaPipe's Face Landmarker already outputs as part of its normal 478-point
# face mesh (indices 468-477) -- no extra model or detector needed, this just
# reads more out of the same face_landmarks list gaze_engine.py already has
# every frame.
#
# How it works: compares the iris center's position to the eye's corners/lids
# to get a 0-1 ratio per axis (0.5 = centered in the eye socket). This is
# gaze *within the eye socket*, independent of head rotation -- e.g. it can
# tell you glanced sideways at your phone without turning your head.
#
# Note: MediaPipe's internal "left eye"/"right eye" labeling is the subject's
# anatomical left/right, which reads backwards on a mirrored webcam preview.
# The two eyes are just labeled A/B below to avoid asserting a left/right
# that hasn't been checked against a real camera -- if "Left"/"Right" ever
# come out backwards once you test this live, swap the two branches in
# get_gaze_direction()'s h_ratio check.

EYE_A_IRIS = 468
EYE_A_OUTER, EYE_A_INNER = 33, 133
EYE_A_TOP, EYE_A_BOTTOM = 159, 145

EYE_B_IRIS = 473
EYE_B_OUTER, EYE_B_INNER = 263, 362
EYE_B_TOP, EYE_B_BOTTOM = 386, 374

# ---- tunable ----
GAZE_HORIZONTAL_DEADZONE = 0.15  # distance from 0.5 (center) the h_ratio must clear to count as looking left/right
GAZE_VERTICAL_DEADZONE = 0.15    # same, for up/down


def _ratio(value, a, b):
    lo, hi = (a, b) if a <= b else (b, a)
    if hi - lo < 1e-6:
        return 0.5
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def _eye_ratios(landmarks, iris_idx, outer_idx, inner_idx, top_idx, bottom_idx):
    iris = landmarks[iris_idx]
    outer, inner = landmarks[outer_idx], landmarks[inner_idx]
    top, bottom = landmarks[top_idx], landmarks[bottom_idx]
    h_ratio = _ratio(iris.x, outer.x, inner.x)
    v_ratio = _ratio(iris.y, top.y, bottom.y)
    return h_ratio, v_ratio


def get_gaze_direction(landmarks):
    """landmarks = face_landmarks[0] (the 478-point list) from a FaceLandmarker
    result. Returns (direction, h_ratio, v_ratio):
      direction: "Center", "Left", "Right", "Up", or "Down"
      h_ratio, v_ratio: 0.0-1.0, 0.5 = centered -- useful for on-screen debugging
    """
    ah, av = _eye_ratios(landmarks, EYE_A_IRIS, EYE_A_OUTER, EYE_A_INNER, EYE_A_TOP, EYE_A_BOTTOM)
    bh, bv = _eye_ratios(landmarks, EYE_B_IRIS, EYE_B_OUTER, EYE_B_INNER, EYE_B_TOP, EYE_B_BOTTOM)
    h_ratio = (ah + bh) / 2
    v_ratio = (av + bv) / 2

    direction = "Center"
    if h_ratio < 0.5 - GAZE_HORIZONTAL_DEADZONE:
        direction = "Left"
    elif h_ratio > 0.5 + GAZE_HORIZONTAL_DEADZONE:
        direction = "Right"
    elif v_ratio < 0.5 - GAZE_VERTICAL_DEADZONE:
        direction = "Up"
    elif v_ratio > 0.5 + GAZE_VERTICAL_DEADZONE:
        direction = "Down"

    return direction, h_ratio, v_ratio
