# Encrypted local persistence for Study Warden's "debt accumulation"
# feature set (see memory/feedback_engagement_judgment.md for the full
# multi-piece story). Two independent things live in the same file:
#
# - A DEBT LEDGER, keyed by course/field: how far into a CALENDAR-LINKED
#   study session (auto-picked from the Study Planner's schedule -- NOT
#   a Revision/forced session, which has nothing on the calendar to
#   carry over) you'd gotten when you left it early, plus the shortfall
#   (planned-but-never-happened) study time. Saved on every mid-session
#   lockdown exit for a calendar-linked session, cleared once that
#   course/field's debt gets folded into a fresh session's total time
#   (see main_window.py's _begin_lockdown_flow()) -- keyed per course/
#   field (not a single slot) since debt for one course shouldn't get
#   silently lost the moment you leave a DIFFERENT course's session
#   early too.
# - A PENALTY DEBT: how many push-ups were still owed if the app closed
#   (or lockdown exited) while a pose-challenge penalty was actively in
#   progress. Cleared once those reps actually get completed (see
#   main_window.py's PenaltyCompletionDialog).
#
# Encrypted via Windows DPAPI (pywin32's win32crypt), not a hand-rolled
# cipher -- ties decryption to the CURRENT WINDOWS USER LOGIN, so there's
# no separate key file to manage or leave lying around next to the data
# it protects. Needs `pip install pywin32` if not already present --
# Windows-only, matching the rest of this app (lockdown_os.py is already
# Windows-specific).

import json
from datetime import datetime

import win32crypt

from paths import get_app_data_dir

SNAPSHOT_PATH = get_app_data_dir() / ".session_snapshot.enc"
# Extra context mixed into DPAPI's own encryption -- not a secret (DPAPI's real protection comes from the
# Windows user login), just namespaces it so nothing else that happens to also use DPAPI on this machine
# could accidentally decrypt this, or vice versa.
DPAPI_ENTROPY = b"StudyWarden.session_snapshot.v1"


def _debt_key(course, field):
    return f"{course}|{field}"


# ---- debt ledger (per course/field) ----

def save_debt(course, field, pdf_path, slide_number, total_slides, timer_widget):
    """Records/overwrites the debt entry for this course/field with the
    CURRENT state of a mid-session lockdown exit. slide_number/
    total_slides give completion %; shortfall_seconds (total_study_
    seconds - study_seconds_elapsed) is the actual "debt" a later piece
    folds into that course/field's next session. Best-effort: a save
    failure is logged, not raised -- losing this record means losing
    debt-tracking for this exit, not anything the CURRENT session needs
    to keep running."""
    completion_pct = round((slide_number / total_slides) * 100, 1) if total_slides > 0 else 0.0
    total_seconds = timer_widget.total_study_seconds()
    elapsed_seconds = timer_widget.study_seconds_elapsed()
    entry = {
        "course": course,
        "field": field,
        "pdf_path": str(pdf_path) if pdf_path else None,
        "slide_number": slide_number,
        "total_slides": total_slides,
        "completion_pct": completion_pct,
        "block_index": timer_widget.block_index,
        "blocks_total": len(timer_widget.blocks),
        "time_left_seconds": timer_widget.time_left,
        "total_study_seconds": total_seconds,
        "study_seconds_elapsed": elapsed_seconds,
        "shortfall_seconds": max(0, total_seconds - elapsed_seconds),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    _mutate(lambda data: data["debts"].__setitem__(_debt_key(course, field), entry), "save debt")


def clear_debt(course, field):
    """Called once this course/field's debt has been folded into a fresh
    session's total time, or that session finished naturally with
    nothing left owed."""
    _mutate(lambda data: data["debts"].pop(_debt_key(course, field), None), "clear debt")


def load_debt(course, field):
    """Returns this course/field's outstanding debt entry, or None."""
    return _load_all().get("debts", {}).get(_debt_key(course, field))


def load_all_debts():
    """Every outstanding debt entry, course/field-keyed -- e.g. for a
    day-rollover check that needs to look at ALL of them, not just one
    specific course/field (see study_planner.roll_over_incomplete_day())."""
    return dict(_load_all().get("debts", {}))


# ---- penalty debt (reps owed) ----

def save_penalty(reps_owed):
    """reps_owed: how many push-ups were still unfinished when a penalty
    got interrupted (lockdown exited, or the app closed, mid-PUSHUPS)."""
    if reps_owed <= 0:
        return
    entry = {"reps_owed": reps_owed, "saved_at": datetime.now().isoformat(timespec="seconds")}
    _mutate(lambda data: data.__setitem__("penalty", entry), "save penalty debt")


def clear_penalty():
    """Called once the owed reps actually get completed."""
    _mutate(lambda data: data.__setitem__("penalty", None), "clear penalty debt")


def load_penalty():
    """Returns {"reps_owed", "saved_at"}, or None if nothing's owed."""
    return _load_all().get("penalty")


# ---- storage ----

def _load_all():
    """The whole {"debts": {...}, "penalty": {...} or None} structure --
    never raises, an unreadable/missing/corrupted file is just treated as
    "nothing outstanding" rather than crashing whatever's checking it."""
    if not SNAPSHOT_PATH.exists():
        return {"debts": {}, "penalty": None}
    try:
        encrypted = SNAPSHOT_PATH.read_bytes()
        _description, plaintext = win32crypt.CryptUnprotectData(encrypted, DPAPI_ENTROPY, None, None, 0)
        data = json.loads(plaintext.decode("utf-8"))
        data.setdefault("debts", {})
        data.setdefault("penalty", None)
        return data
    except Exception as e:
        print(f"[session_snapshot] couldn't load (treating as nothing outstanding): {e}")
        return {"debts": {}, "penalty": None}


def _mutate(fn, action_label):
    """Read-modify-write the whole structure -- fn mutates the dict in
    place. Best-effort: a write failure is logged, not raised."""
    try:
        data = _load_all()
        fn(data)
        plaintext = json.dumps(data).encode("utf-8")
        encrypted = win32crypt.CryptProtectData(plaintext, "Study Warden session snapshot", DPAPI_ENTROPY, None, None, 0)
        SNAPSHOT_PATH.write_bytes(encrypted)
    except Exception as e:
        print(f"[session_snapshot] couldn't {action_label}: {e}")
