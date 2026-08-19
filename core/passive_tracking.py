# Lightweight, LOCAL-ONLY time tracking for non-exam work (to-dos,
# assignments, projects) -- deliberately separate from Monitor Lockdown's
# whole webcam/distraction-enforcement pipeline. "Passive" means exactly
# that: a plain start/stop stopwatch with no monitoring, no penalties,
# just an honest log of how long you worked on something (see ui/main_
# window.py's WeekSettingsWidget, the "To-Do List" tab).
#
# Stored as plain JSON, NOT DPAPI-encrypted like session_snapshot.py's
# debt ledger -- this is just a work log, not the kind of accountability
# data that ledger deliberately protects.

import json
from datetime import date, datetime

from paths import get_app_data_dir

LOG_PATH = get_app_data_dir() / ".passive_tracking_log.json"


def _load_log():
    if not LOG_PATH.exists():
        return []
    try:
        return json.loads(LOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _save_log(entries):
    LOG_PATH.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def log_session(description, minutes, on_date=None):
    """Appends one completed passive-tracking session to the local log.
    minutes: float -- the timer itself tracks real elapsed seconds, this
    just rounds to 1 decimal for a readable log. Skips logging entirely
    if that ROUNDED value is <=0 (a session under ~3 seconds), not just
    a raw non-positive input -- otherwise a stray immediate start/stop
    would leave a confusing "0.0 min" entry in the log."""
    rounded = round(minutes, 1)
    if rounded <= 0:
        return
    entries = _load_log()
    entries.append({
        "date": (on_date or date.today()).isoformat(),
        "description": (description or "").strip() or "(untitled)",
        "minutes": rounded,
        # Microsecond resolution, not just seconds -- two sessions logged
        # within the same second (e.g. quick back-to-back tracking) would
        # otherwise tie and make list_entries()'s "most recent first"
        # ordering ambiguous.
        "logged_at": datetime.now().isoformat(timespec="microseconds"),
    })
    _save_log(entries)


def todays_total_minutes(on_date=None):
    """Sum of minutes logged for on_date (default today) -- what the
    To-Do List tab shows as "Today's passive time"."""
    target = (on_date or date.today()).isoformat()
    return sum(e["minutes"] for e in _load_log() if e["date"] == target)


def list_entries(limit=None):
    """Most-recent-first log entries, optionally capped to the last
    `limit` -- for a simple history view."""
    entries = sorted(_load_log(), key=lambda e: e["logged_at"], reverse=True)
    return entries[:limit] if limit else entries
