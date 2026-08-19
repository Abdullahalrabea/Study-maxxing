# Phone-holding detector for Study Warden. Uses MediaPipe's Object Detector
# (EfficientDet-Lite0, trained on COCO's 80 classes).
#
# Needs, next to this file:
#   vision/efficientdet_lite0.tflite
#     https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/int8/1/efficientdet_lite0.tflite
#
# Note: this is a plain .tflite file, not a .task bundle like the other
# models in this project -- that's expected, BaseOptions accepts either.
#
# IF DETECTION FEELS UNRELIABLE: EfficientDet-Lite0 is the smallest/fastest
# model in this family, runs on a downscaled 320x320 input, and a phone held
# in your hand is a small, often partially-occluded object -- it just doesn't
# score very high most of the time, even when it's right. Two things to try:
#   1. Lower PHONE_SCORE_THRESHOLD below (it's already been dropped from an
#      initial 0.5 to 0.35 for this reason).
#   2. Swap in the bigger, more accurate EfficientDet-Lite2 model (448x448
#      input): download
#      https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite2/float32/1/efficientdet_lite2.tflite
#      as "efficientdet_lite2.tflite" and point PHONE_MODEL_PATH in
#      gaze_engine.py at it instead. It's slower per-frame, but this only
#      runs during MONITORING, so it shouldn't be a problem.
#   (If that exact lite2 URL 404s, search "mediapipe object detector models"
#   for the current link -- Google occasionally reorganizes these.)
#
# gaze_engine.py draws every detection it gets back (label + score box) during
# MONITORING so you can see exactly what the model is calling your phone --
# e.g. it may be scoring it as "remote" instead, or scoring "cell phone"
# right but just under the threshold.

from pathlib import Path

from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import ObjectDetector, ObjectDetectorOptions, RunningMode

# ---- tunable ----
MIN_REPORT_SCORE = 0.2        # floor for even returning a detection (kept low for debug visibility)
PHONE_SCORE_THRESHOLD = 0.35  # confidence required to actually count as "phone visible"

# COCO labels that count as "holding a phone". "remote" is included because
# COCO-trained models very commonly confuse a phone with a TV remote (both
# are small dark rectangles held in a hand) -- remove it from this set if
# that causes false triggers from an actual remote or similar object nearby.
PHONE_LIKE_LABELS = {"cell phone", "remote"}


class Detection:
    """One detected object: label, confidence score, and (x, y, w, h) pixel bounding box."""

    def __init__(self, label, score, bbox):
        self.label = label
        self.score = score
        self.bbox = bbox


class PhoneDetector:
    """Usage:
        with PhoneDetector(PHONE_MODEL_PATH) as phone_detector:
            detections = phone_detector.get_detections(mp_image, timestamp_ms)
            visible = phone_detector.is_phone_visible(detections)
    """

    def __init__(self, model_path):
        self.model_path = Path(model_path)
        self._detector = None

    def __enter__(self):
        options = ObjectDetectorOptions(
            base_options=BaseOptions(model_asset_path=str(self.model_path)),
            running_mode=RunningMode.VIDEO,
            max_results=5,
            score_threshold=MIN_REPORT_SCORE,
            # no category_allowlist on purpose -- see the module docstring;
            # we want visibility into everything the model detects, not just
            # a silently-filtered "cell phone" or nothing.
        )
        self._detector = ObjectDetector.create_from_options(options)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._detector is not None:
            self._detector.close()
            self._detector = None

    def get_detections(self, mp_image, timestamp_ms):
        """All objects detected this frame (not just phones) -- call this once
        per frame, then derive both the on-screen debug boxes and the phone
        check from the same result (calling the model twice per frame with
        the same timestamp isn't allowed in VIDEO mode)."""
        result = self._detector.detect_for_video(mp_image, timestamp_ms)
        detections = []
        for d in result.detections:
            if not d.categories:
                continue
            top = d.categories[0]
            bbox = (d.bounding_box.origin_x, d.bounding_box.origin_y,
                    d.bounding_box.width, d.bounding_box.height)
            detections.append(Detection(top.category_name, top.score, bbox))
        return detections

    def is_phone_visible(self, detections):
        """Pass in the list from get_detections() -- does not call the model again."""
        return any(d.label in PHONE_LIKE_LABELS and d.score >= PHONE_SCORE_THRESHOLD
                   for d in detections)
