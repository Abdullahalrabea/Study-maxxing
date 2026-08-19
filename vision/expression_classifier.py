# Facial-expression classifier for Study Warden. Turns the 52 ARKit-style
# face blendshapes that FaceLandmarker already computes (the same dict
# gaze_engine.py builds for blink/yawn detection) into one readable label.
# No extra model needed -- purely a rule-based reading of existing data.
#
# This is approximate and rule-based, not trained/validated against real
# faces -- treat the label as a rough signal, and adjust the thresholds
# below (or add new expressions) if it doesn't match what you'd call it.

# {expression name: {blendshape_name: minimum_score, ...}} -- same format as
# gesture_config.py's "face" entries, reused here for a general-purpose label
# instead of one specific pose. ALL listed blendshapes must be roughly met.
EXPRESSIONS = {
    "Happy":     {"mouthSmileLeft": 0.4, "mouthSmileRight": 0.4},
    "Surprised": {"eyeWideLeft": 0.3, "eyeWideRight": 0.3, "browInnerUp": 0.3, "jawOpen": 0.15},
    "Sad":       {"mouthFrownLeft": 0.3, "mouthFrownRight": 0.3},
    "Angry":     {"browDownLeft": 0.4, "browDownRight": 0.4},
    "Tired":     {"eyeSquintLeft": 0.4, "eyeSquintRight": 0.4},
}

NEUTRAL_LABEL = "Neutral"
MIN_CONFIDENCE = 0.55  # below this, report Neutral instead of a weak/ambiguous guess


def _score(blendshape_scores, requirements):
    parts = []
    for name, threshold in requirements.items():
        actual = blendshape_scores.get(name, 0.0)
        parts.append(min(1.0, actual / threshold) if threshold > 0 else 1.0)
    return sum(parts) / len(parts) if parts else 0.0


def classify_expression(blendshape_scores):
    """blendshape_scores: the {category_name: score} dict gaze_engine.py
    already builds each frame. Returns (label, confidence 0.0-1.0)."""
    if not blendshape_scores:
        return NEUTRAL_LABEL, 0.0

    best_label, best_score = NEUTRAL_LABEL, 0.0
    for label, requirements in EXPRESSIONS.items():
        s = _score(blendshape_scores, requirements)
        if s > best_score:
            best_label, best_score = label, s

    if best_score < MIN_CONFIDENCE:
        return NEUTRAL_LABEL, best_score
    return best_label, best_score
