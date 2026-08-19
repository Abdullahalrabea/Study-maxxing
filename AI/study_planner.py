# Turns "I have a quiz/midterm with N chapters" (or "I have a project/
# assignment due") into an actual day-by-day calendar: reads the real
# deadline from the existing schedule table (notion_table_client.py),
# estimates total minutes needed from uploaded material (+ chapter count for
# exams, or a free-text description for projects/assignments) via the LLM,
# works out how many days that takes at your daily time budget, and writes
# one calendar entry per day to the "To Do Date" database -- but only once
# you've reviewed the plan and asked it to commit; generate_plan() never
# writes anything by itself.
#
# Scheduling treats every day as having two independent slots -- one STUDY
# slot (exam review) and one WORK slot (an assignment or project) -- rather
# than "free" vs "full", so a day can hold one of each but not two of the
# same kind. It also pulls every OTHER exam/assignment/project across every
# course (list_exams() is already course-agnostic) plus everything already
# committed to the calendar, and spreads the days it needs evenly across the
# whole runway to the deadline instead of cramming them all into the final
# stretch -- see _spread_target_days() / _pick_session_days().

import re
from datetime import date, datetime, timedelta

import syllabus_parser
from llm_client import LLMClient
from notion_client import NotionClient
from notion_table_client import NotionTableClient

_ORDINAL_RE = re.compile(r"(\d+)(st|nd|rd|th)\b", re.IGNORECASE)

MAX_TOTAL_IMAGES = 4  # across all attached files combined -- a dozen full-page renders was observed to
                       # blow past REQUEST_TIMEOUT_SEC on modest local hardware before even finishing
                       # prompt processing (vision models often need >=1000+ tokens per image just to
                       # encode it), so start conservative -- llm_client._chat_multimodal() halves further
                       # and retries on its own if even this many still times out or overflows context

# Deadlines where "how much material/effort" matters more than "how many
# chapters" -- these get a free-text description instead of a chapter count.
# Must match row labels in notion_table_client.ROW_LABELS.
WORK_FIELD_LABELS = {"Assignments", "Assignments-2", "Certificate or Coursera", "Project", "Project-2"}

# Every generated calendar entry's title starts with one of these, so
# _classify_committed_entries() can tell a study session from a work session
# just by reading back what's already on the calendar.
STUDY_LABEL_PREFIX = "Study "
WORK_LABEL_PREFIX = "Work: "


class StudyPlannerError(Exception):
    pass


def _parse_humanized_date(text, today=None):
    """Parses a humanize_date()-produced string like "April 5th" or "April
    5th at 2:00 PM" back into a real date. There's no year in the stored
    text, so this picks whichever of this-year/next-year lands in the
    future relative to today (an exam you're prepping for is never in the
    past). Returns None for anything that doesn't look like a real date
    (blank, "TBA", "N/A", unparseable)."""
    if not text or not text.strip():
        return None
    today = today or date.today()

    cleaned = _ORDINAL_RE.sub(r"\1", text.strip())
    cleaned = re.split(r"\s+at\s+", cleaned, maxsplit=1)[0]  # drop a trailing time, if any

    candidates = []
    for year in (today.year, today.year + 1):
        try:
            candidates.append(datetime.strptime(f"{cleaned} {year}", "%B %d %Y").date())
        except ValueError:
            continue
    if not candidates:
        return None
    future = [d for d in candidates if d >= today]
    return min(future) if future else min(candidates)


def _split_into_groups(n, k):
    """Splits chapters 1..n into k consecutive groups, as evenly-sized as
    possible (earlier groups get any remainder). Returns a list of length
    k, each item an (start, end) inclusive 1-based chapter range, or None
    for a group that gets no chapters (when k > n -- an extra review day)."""
    if n <= 0:
        return [None] * k
    base, remainder = divmod(n, k)
    ranges = []
    current = 1
    for i in range(k):
        size = base + (1 if i < remainder else 0)
        if size == 0:
            ranges.append(None)
            continue
        ranges.append((current, current + size - 1))
        current += size
    return ranges


def _chapter_label(chapter_range):
    if chapter_range is None:
        return "Review"
    start, end = chapter_range
    if start == end:
        return f"chap {start}"
    if end == start + 1:
        return f"chap {start} & {end}"
    return f"chap {start}-{end}"


def _work_label(index, total):
    if total <= 1:
        return "work session"
    return f"work session (day {index} of {total})"


def _match_label_to_deadline(label, exams):
    """Given a committed calendar entry's title -- which this module always
    writes as "{STUDY_LABEL_PREFIX}{course} {field} ..." or
    "{WORK_LABEL_PREFIX}{course} {field} ..." -- figures out which known
    deadline (from list_exams()) it belongs to. Returns the matching exam
    dict, or None if it can't be matched (a hand-added entry, or the
    matching course/field was since removed from the schedule table). Tries
    longer course+field combos first so e.g. "CS223" can't wrongly
    prefix-match before "CS223 Lab" when both exist."""
    if label.startswith(STUDY_LABEL_PREFIX):
        rest = label[len(STUDY_LABEL_PREFIX):]
    elif label.startswith(WORK_LABEL_PREFIX):
        rest = label[len(WORK_LABEL_PREFIX):]
    else:
        return None
    for exam in sorted(exams, key=lambda e: -len(f"{e['course']} {e['field']}")):
        if rest.startswith(f"{exam['course']} {exam['field']}"):
            return exam
    return None


def _classify_committed_entries(entries, exams):
    """entries: [{"date": "YYYY-MM-DD", "title": str}, ...] from
    NotionClient.list_calendar_entries(). Returns (study_days, work_days,
    day_priority):
    - study_days / work_days: the sets of dates that already have a
      STUDY-slot / WORK-slot session committed, read off each title's
      prefix (see STUDY_LABEL_PREFIX / WORK_LABEL_PREFIX). An entry that
      matches neither -- e.g. added by hand, or from before this prefix
      convention existed -- is treated as occupying BOTH slots, the
      conservative assumption when we can't tell what it is.
    - day_priority: {date: earliest deadline date of whatever's already
      committed that day} -- for EDF: when a new plan needs to fall back
      onto an already-used day, this says whether the thing already there
      is MORE urgent (protect it) or LESS urgent (fair game) than the new
      plan's own deadline. Days with nothing matchable simply aren't keyed."""
    study_days, work_days = set(), set()
    day_priority = {}
    for entry in entries:
        try:
            d = datetime.strptime(entry["date"][:10], "%Y-%m-%d").date()
        except (ValueError, TypeError, KeyError):
            continue
        title = entry.get("title") or ""
        if title.startswith(STUDY_LABEL_PREFIX):
            study_days.add(d)
        elif title.startswith(WORK_LABEL_PREFIX):
            work_days.add(d)
        else:
            study_days.add(d)
            work_days.add(d)

        owner = _match_label_to_deadline(title, exams)
        if owner is not None:
            if d not in day_priority or owner["date"] < day_priority[d]:
                day_priority[d] = owner["date"]
    return study_days, work_days, day_priority


def _spread_target_days(today, deadline, days_needed):
    """days_needed dates, evenly spaced across [today, deadline-1] -- the
    "ideal" placement before any slot-conflict adjustment, so sessions land
    throughout the whole runway (roughly a spaced-repetition cadence ending
    near the deadline) instead of all cramming into the final days when
    there's plenty of lead time. If the window is too short to fit
    days_needed distinct days, just returns every day in it."""
    last_day = deadline - timedelta(days=1)
    window = (last_day - today).days + 1
    if window <= 0 or days_needed <= 0:
        return []
    if days_needed >= window:
        return [today + timedelta(days=i) for i in range(window)]
    targets = []
    for i in range(days_needed):
        offset = round((i + 1) * window / days_needed) - 1
        offset = max(0, min(offset, window - 1))
        targets.append(today + timedelta(days=offset))
    return targets


def _nearest_free_day(target, today, last_day, is_free):
    """Searches outward from `target` (staying within [today, last_day])
    for the closest day where is_free(day) is True. Returns None if there
    isn't one anywhere in range."""
    if is_free(target):
        return target
    span = (last_day - today).days
    radius = 1
    while radius <= span:
        for candidate in (target - timedelta(days=radius), target + timedelta(days=radius)):
            if today <= candidate <= last_day and is_free(candidate):
                return candidate
        radius += 1
    return None


def _pick_session_days(today, deadline, days_needed, is_work_type, study_days, work_days, avoid_days,
                        day_priority):
    """Chooses days_needed session days for this plan: spread evenly across
    [today, deadline-1] (_spread_target_days) rather than crammed at the
    end, each nudged to the nearest usable day. "Usable" is checked in three
    tiers, tried in order for every target day (greedy, earliest-deadline-
    first: this plan's OWN deadline decides how far down the tiers it's
    willing to go):
      0. Free -- no session of the SAME slot type there yet, and not
         another deadline's own due date (avoid_days).
      1. Occupied, but by something with a deadline no more urgent than
         this plan's own (day_priority.get(d) is None or >= deadline) --
         doubling up here doesn't bump anything more urgent.
      2. Occupied by something MORE urgent (an earlier deadline) -- last
         resort only, when the window is genuinely full even at tier 1.
    Returns (sorted_dates, used_tier1, used_tier2). used_tier1 means some
    session doubled up with an equal-or-lower-priority commitment (expected,
    unremarkable EDF behavior). used_tier2 means some session had to bump
    into something MORE urgent than itself (a genuine "not enough runway"
    situation worth a real warning)."""
    last_day = deadline - timedelta(days=1)
    if last_day < today:
        return [], False, False

    claimed = set()  # days this plan itself has already used, this call
    occupied = work_days if is_work_type else study_days

    def usable(d, tier):
        if d in avoid_days or d in claimed:
            return False
        if tier == 0:
            return d not in occupied
        if tier == 1:
            owner_deadline = day_priority.get(d)
            return owner_deadline is None or owner_deadline >= deadline
        return True  # tier 2: anything goes, last resort

    used_tier1 = False
    used_tier2 = False
    chosen = []
    for target in _spread_target_days(today, deadline, days_needed):
        day = None
        for tier in (0, 1, 2):
            day = _nearest_free_day(target, today, last_day, lambda d, t=tier: usable(d, t))
            if day is not None:
                if tier == 1:
                    used_tier1 = True
                elif tier == 2:
                    used_tier2 = True
                break
        if day is None:
            continue  # no room left anywhere in the window, even at the last resort
        chosen.append(day)
        claimed.add(day)

    chosen.sort()
    return chosen, used_tier1, used_tier2


class StudyPlanner:
    def __init__(self, notion_token, table_block_id, calendar_database_id, llm_client=None):
        self.notion = NotionTableClient(notion_token, table_block_id)
        self.notion_client = NotionClient(notion_token)
        self.calendar_database_id = calendar_database_id
        self.llm = llm_client or LLMClient()

    def list_exams(self, today=None):
        """Returns [{"course", "field", "date_text", "date"}, ...] for
        every course/field in the schedule table that currently holds a
        real, parseable date (skips blank/TBA/N-A) -- for populating an
        exam picker."""
        today = today or date.today()
        state = self.notion.get_state()
        exams = []
        for col in range(state.num_columns):
            course = state.course_name(col)
            if not state.is_filled(course):
                continue
            for field in state.field_names():
                row = state.row_index_for_field(field)
                date_text = state.grid[row][col]
                parsed = _parse_humanized_date(date_text, today=today)
                if parsed is None:
                    continue
                exams.append({
                    "course": course,
                    "field": field,
                    "date_text": date_text,
                    "date": parsed,
                })
        return exams

    def todays_sessions(self, today=None):
        """Returns [{"course", "field", "date_text", "date"}, ...] -- the
        known deadline (see list_exams()) each committed calendar entry for
        TODAY belongs to, matched via _match_label_to_deadline(). Used for
        "what should I be working on right now" lookups (e.g. Monitor
        Lockdown picking today's material) -- returns one entry per
        distinct course/field, even if that plan wrote several sessions for
        the same day (shouldn't normally happen, but not assumed)."""
        today = today or date.today()
        exams = self.list_exams(today=today)
        entries = self.notion_client.list_calendar_entries(self.calendar_database_id)
        matches = []
        seen = set()
        for entry in entries:
            try:
                d = datetime.strptime(entry["date"][:10], "%Y-%m-%d").date()
            except (ValueError, TypeError, KeyError):
                continue
            if d != today:
                continue
            match = _match_label_to_deadline(entry.get("title") or "", exams)
            if match is not None and (match["course"], match["field"]) not in seen:
                seen.add((match["course"], match["field"]))
                matches.append(match)
        return matches

    def generate_plan(self, course, field, chapter_count=None, description=None, material_paths=None,
                       daily_minutes=100, today=None):
        """Builds a day-by-day plan (does NOT write anything to Notion --
        see commit_plan()). For an exam/quiz/etc field, pass chapter_count;
        for a field in WORK_FIELD_LABELS (Assignments/Assignments-2/
        Certificate or Coursera/Project/Project-2), pass description
        instead (a free-text summary of what it needs --
        chapters don't apply) -- chapter_count is ignored for those and
        description is ignored otherwise. material_paths is a list of file
        paths (or None) -- text/images are extracted from each and combined
        before estimating total time needed.

        Session days are spread evenly across the whole runway to the
        deadline rather than crammed into the final stretch, and each is
        chosen with awareness of the rest of your schedule: every OTHER
        exam/assignment/project across every course, and everything already
        committed to the calendar, are treated as occupying that day's STUDY
        or WORK slot (a day can hold one of each, not two of the same kind)
        and avoided when possible -- see _pick_session_days. Only reused if
        there truly isn't enough free runway before the deadline.

        Returns {"course", "field", "exam_date", "total_minutes",
        "days_needed", "sessions": [{"date": date, "label": str}, ...],
        "warning": str or None}. Raises StudyPlannerError if the
        course/field's deadline can't be found or isn't a real date yet."""
        is_work_type = field in WORK_FIELD_LABELS
        if not is_work_type and (chapter_count is None or chapter_count <= 0):
            raise StudyPlannerError("Chapter count must be at least 1.")
        if daily_minutes <= 0:
            raise StudyPlannerError("Daily study minutes must be at least 1.")

        today = today or date.today()
        exams = self.list_exams(today=today)
        match = next((e for e in exams if e["course"] == course and e["field"] == field), None)
        if match is None:
            raise StudyPlannerError(
                f"No real date found for {course} {field} -- make sure it's filled in on the "
                "schedule table (not blank/TBA/N-A) before generating a plan for it."
            )
        deadline = match["date"]

        material_text = ""
        material_images = []
        if material_paths:
            texts = []
            for path in material_paths:
                text, images = syllabus_parser.extract_text_and_images(path)
                if text:
                    texts.append(text)
                material_images.extend(images)
            material_text = "\n\n".join(texts)
            material_images = material_images[:MAX_TOTAL_IMAGES]

        if is_work_type:
            total_minutes = self.llm.estimate_project_minutes(description, material_text, images_b64=material_images)
        else:
            total_minutes = self.llm.estimate_study_minutes(material_text, chapter_count, images_b64=material_images)
        days_needed = -(-total_minutes // daily_minutes)  # ceil division

        # Topic extraction happens here -- at plan-generation time, which
        # you're already waiting on the estimate call above for -- rather
        # than at Monitor Lockdown time, where the same LLM call (often
        # much slower for image-heavy material) would otherwise run the
        # focus timer down before it even finished. Best-effort: a failed
        # extraction shouldn't block plan generation itself.
        topics = []
        if material_text or material_images:
            try:
                topics = self.llm.extract_topics(material_text, images_b64=material_images)
            except Exception:
                topics = []

        avoid_days = {
            e["date"] for e in exams if not (e["course"] == course and e["field"] == field)
        }
        entries = self.notion_client.list_calendar_entries(self.calendar_database_id)
        study_days, work_days, day_priority = _classify_committed_entries(entries, exams)

        session_dates, used_tier1, used_tier2 = _pick_session_days(
            today, deadline, days_needed, is_work_type, study_days, work_days, avoid_days, day_priority
        )

        warning = None
        slot = "work" if is_work_type else "study"
        if len(session_dates) < days_needed:
            noun = "work" if is_work_type else "study"
            warning = (
                f"Only {len(session_dates)} day(s) available before the deadline, but "
                f"{days_needed} day(s) of {noun} are needed at {daily_minutes} min/day -- "
                "consider raising your daily time budget."
            )
            days_needed = len(session_dates)
        elif used_tier2:
            warning = (
                f"Some sessions had to double up on a day already claimed by a MORE urgent deadline "
                f"(due sooner than this one) -- there wasn't enough free or lower-priority runway to "
                f"avoid it. Consider raising your daily time budget or checking that deadline's own plan."
            )
        elif used_tier1:
            warning = (
                f"Some sessions share a day with another, less-urgent {slot} session already on the "
                "calendar -- that's expected when things are due close together, just know those days "
                "carry a heavier load."
            )

        if is_work_type:
            sessions = [
                {"date": session_date, "label": f"{WORK_LABEL_PREFIX}{course} {field} "
                                                 f"{_work_label(i + 1, len(session_dates))}"}
                for i, session_date in enumerate(session_dates)
            ]
        else:
            chapter_ranges = _split_into_groups(chapter_count, len(session_dates)) if session_dates else []
            sessions = [
                {"date": session_date,
                 "label": f"{STUDY_LABEL_PREFIX}{course} {field} {_chapter_label(chapter_range)}"}
                for session_date, chapter_range in zip(session_dates, chapter_ranges)
            ]

        return {
            "course": course,
            "field": field,
            "exam_date": deadline,
            "total_minutes": total_minutes,
            "days_needed": days_needed,
            "sessions": sessions,
            "warning": warning,
            "topics": topics,
        }

    def commit_plan(self, sessions):
        """Writes each session as a new row in the calendar database.
        sessions: the "sessions" list from generate_plan() (or an edited
        copy of it)."""
        for session in sessions:
            self.notion_client.add_calendar_entry(
                self.calendar_database_id, session["label"], session["date"].isoformat()
            )

    def roll_over_incomplete_day(self, course, field, from_date, to_date):
        """Moves a committed calendar entry for course/field that was on
        from_date to to_date instead -- called when core/session_
        snapshot.py's debt ledger shows that day's session was left
        incomplete and the day has since passed (see main_window.py's
        startup check). Finds the specific entry by date + the SAME
        "{STUDY_LABEL_PREFIX}{course} {field}..." title convention
        commit_plan() itself writes (see _match_label_to_deadline()) --
        moves the FIRST match only, since generate_plan() writes one
        entry per day for a given course/field, never more than one on
        the same day. Returns True if an entry was actually moved, False
        if none was found on from_date for that course/field (e.g. it
        was already moved, or was never actually committed to begin
        with) -- a no-op either way, not an error, since a caller
        checking several outstanding debts shouldn't have one missing
        entry break the rest."""
        prefix = f"{STUDY_LABEL_PREFIX}{course} {field}"
        entries = self.notion_client.list_calendar_entries(self.calendar_database_id)
        for entry in entries:
            if entry["date"][:10] != from_date.isoformat():
                continue
            if not entry["title"].startswith(prefix):
                continue
            self.notion_client.update_calendar_entry(
                self.calendar_database_id, entry["id"], date_str=to_date.isoformat()
            )
            return True
        return False

    def mark_session_completed(self, course, field, on_date):
        """Archives (Notion's own recoverable delete -- see notion_
        client.delete_calendar_entry()) the committed calendar entry for
        course/field that's on on_date -- called when Monitor Lockdown
        detects the material hit 100% completion (see LockdownWindow),
        removing that day's now-done item from the calendar rather than
        leaving it sitting there looking outstanding. There's no
        separate "Completed" status property anywhere in this schedule
        table/calendar -- the calendar is a plain title+date list, so
        "done" is represented by the entry being gone, the same way
        finishing a to-do item removes it, not by flipping a status
        field that doesn't exist. Same find-by-date+title-prefix approach
        as roll_over_incomplete_day(); returns True if an entry was
        actually found and archived, False if nothing matched (not an
        error -- e.g. it was never committed to begin with)."""
        prefix = f"{STUDY_LABEL_PREFIX}{course} {field}"
        entries = self.notion_client.list_calendar_entries(self.calendar_database_id)
        for entry in entries:
            if entry["date"][:10] != on_date.isoformat():
                continue
            if not entry["title"].startswith(prefix):
                continue
            self.notion_client.delete_calendar_entry(entry["id"])
            return True
        return False
