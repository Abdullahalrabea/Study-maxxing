# Local, LOCAL-ONLY history of each Monitor Lockdown session's engagement
# data -- distraction counts/duration and the AI's periodic credit
# judgments (see ui/lockdown_window.py's _run_engagement_judgment()/
# get_engagement_summary()). None of this was persisted anywhere before:
# it lived only in memory for the duration of a single session, driving
# the pacing bar in the moment, then discarded once lockdown closed. This
# surfaces it as a plain local history so a simple trend view (see
# ui/engagement_dashboard.py) has real data to show instead of nothing.
#
# Plain JSON, same reasoning as passive_tracking.py -- this isn't
# sensitive accountability data the way session_snapshot.py's debt
# ledger deliberately is.

import json
from datetime import date, timedelta

from paths import get_app_data_dir

LOG_PATH = get_app_data_dir() / ".engagement_log.json"


def _load_log():
    if not LOG_PATH.exists():
        return []
    try:
        return json.loads(LOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _save_log(entries):
    LOG_PATH.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def log_session(course, field, summary, on_date=None):
    """One entry per closed lockdown session. summary: the dict
    LockdownWindow.get_engagement_summary() returns -- {"distraction_
    events", "distracted_seconds", "studied_seconds", "credit_counts"}.
    Skipped entirely if no actual study time elapsed (e.g. a session
    that opened and closed immediately) -- nothing meaningful to log."""
    studied_seconds = summary.get("studied_seconds", 0)
    if studied_seconds <= 0:
        return
    entries = _load_log()
    entries.append({
        "date": (on_date or date.today()).isoformat(),
        "course": course or "",
        "field": field or "",
        "distraction_events": summary.get("distraction_events", 0),
        "distracted_seconds": round(summary.get("distracted_seconds", 0.0), 1),
        "studied_seconds": round(studied_seconds, 1),
        "credit_counts": summary.get("credit_counts", {}),
    })
    _save_log(entries)


def list_entries(since=None):
    """Entries on/after `since` (a date), oldest first -- for a weekly
    view, pass date.today() - timedelta(days=7)."""
    entries = _load_log()
    if since is None:
        return entries
    return [e for e in entries if date.fromisoformat(e["date"]) >= since]


def summarize(entries):
    """Rolls a list of entries (see list_entries()) into one summary
    dict: overall totals plus a per-course breakdown -- what the
    dashboard actually renders. distraction_rate is distracted-seconds
    as a fraction of studied-seconds (0.0 if nothing was studied)."""
    total_studied = sum(e["studied_seconds"] for e in entries)
    total_distracted = sum(e["distracted_seconds"] for e in entries)
    total_distraction_events = sum(e["distraction_events"] for e in entries)

    per_course = {}
    for e in entries:
        key = e["course"] or "(unspecified)"
        bucket = per_course.setdefault(key, {
            "sessions": 0, "studied_seconds": 0.0, "distracted_seconds": 0.0, "distraction_events": 0,
        })
        bucket["sessions"] += 1
        bucket["studied_seconds"] += e["studied_seconds"]
        bucket["distracted_seconds"] += e["distracted_seconds"]
        bucket["distraction_events"] += e["distraction_events"]

    return {
        "total_sessions": len(entries),
        "total_studied_seconds": total_studied,
        "total_distracted_seconds": total_distracted,
        "total_distraction_events": total_distraction_events,
        "distraction_rate": (total_distracted / total_studied) if total_studied > 0 else 0.0,
        "per_course": per_course,
    }


def weekly_summary(today=None):
    """summarize() over the last 7 days (today inclusive) -- the exact
    window the dashboard's default view uses."""
    today = today or date.today()
    return summarize(list_entries(since=today - timedelta(days=6)))


def clear_log():
    """Wipes the whole local engagement history -- e.g. the dashboard's
    own "Reset Progress" button. Irreversible; there's no undo once this
    runs. Local-only data (see this file's own module docstring), so
    this has no effect on anything in Notion."""
    _save_log([])
