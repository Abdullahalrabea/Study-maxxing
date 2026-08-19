# Maps each file in poses/ to the face and/or hand gesture required to
# "solve" it. Kept separate from pose_engine.py so you can add new meme
# images without touching any matching logic.
#
# HOW TO ADD A NEW POSE:
#   1. Drop the image or gif in poses/
#   2. Add an entry below, keyed by the EXACT filename (including extension)
#
# "face": {blendshape_name: minimum_score, ...}  -- ALL listed ones must be
#   met (each is a 0.0-1.0 score, works the same as the blink/yawn detection
#   already in gaze_engine.py). Common names you can use:
#     eyeBlinkLeft, eyeBlinkRight, eyeSquintLeft, eyeSquintRight,
#     eyeWideLeft, eyeWideRight, jawOpen, mouthSmileLeft, mouthSmileRight,
#     mouthFrownLeft, mouthFrownRight, mouthPucker, mouthFunnel,
#     mouthStretchLeft, mouthStretchRight, browDownLeft, browDownRight,
#     browInnerUp, cheekPuff, noseSneerLeft, noseSneerRight
#   OR "face" can just be a named expression string instead of a dict, e.g.
#   "face": "Happy" -- see expression_classifier.py for the list (Happy,
#   Surprised, Sad, Angry, Tired) and to add your own.
#
# "gaze": one of "Center", "Left", "Right", "Up", "Down" -- where your eyes
#   (not your head) need to be looking, via eye_tracker.py.
#
# "hand": one of these gesture names (see pose_engine.py for how each is
#   scored, and to add your own):
#     "near_mouth"       - a hand close to your mouth (like aibaby.jpg)
#     "hands_together"   - both hands brought close together (like a prayer
#                          gesture, or paws-together)
#     "hands_on_face"    - one or both hands covering/near your face
#     "any_hand_visible" - just needs a hand visible at all (easy default)
#
# You can give a file any combination of "face", "gaze", and "hand" -- all of
# the ones present must be satisfied together. A file with no entry here
# falls back to DEFAULT_GESTURE.
#
# These thresholds are starting points, not measured -- watch the live
# "face: NN%  hand: NN%" readout in the challenge window and adjust the
# numbers below if something feels too strict or too easy.

DEFAULT_GESTURE = {"hand": "any_hand_visible"}

GESTURES = {
    # squinting + pursed/puckered lips
    "speedddd.png": {
        "face": {"eyeSquintLeft": 0.4, "eyeSquintRight": 0.4, "mouthPucker": 0.3},
    },
    # big wide grin
    "200w.gif": {
        "face": {"mouthSmileLeft": 0.5, "mouthSmileRight": 0.5},
    },
    # finger/hand up near the mouth
    "aibaby.jpg": {
        "hand": "near_mouth",
    },
    # paws/hands brought together
    "cat-scuba.gif": {
        "hand": "hands_together",
    },
    # praying-hands gesture
    "Florks_RenovaMix___.jpg": {
        "hand": "hands_together",
    },
    # both hands covering the face
    "gethomered.gif": {
        "hand": "hands_on_face",
    },
    # mouth wide open (crying / screaming)
    "shadow.jpg": {
        "face": {"jawOpen": 0.6},
    },
}
