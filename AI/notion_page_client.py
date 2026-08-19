# Manages the "To Do List" Notion page: its title (which encodes the
# current week number, e.g. "To Do List (Current week:15):") and its to-do
# items (plain to_do blocks, appended right after the last existing one so
# the list grows in the order things are added).
#
# This is a different Notion object than notion_table_client.py's table
# block -- that one edits table_row blocks inside a /table; this edits a
# PAGE's own title property, plus to_do blocks that are direct children of
# that page.
#
# SETUP: same integration token as the rest of this project -- the
# integration also needs to be shared with THIS page specifically (Notion's
# "..." menu -> "Connections"), separately from the exams table's page.
#
# GETTING THE PAGE ID: open the page in your browser -- the URL looks like
# notion.so/Some-Title-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX -- the 32-char hex
# string at the end (before any "?") is the page ID.

import re
from datetime import date, timedelta

import requests

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

TITLE_TEMPLATE = "To Do List (Current week:{week}):"
TITLE_PATTERN = re.compile(r"^To Do List \(Current week:(\d+)\):$")


class NotionPageError(Exception):
    pass


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _explain_error(resp):
    try:
        message = resp.json().get("message", resp.text)
    except ValueError:
        message = resp.text
    if resp.status_code == 401:
        return f"Notion rejected the token (401). Detail: {message}"
    if resp.status_code == 404:
        return (
            "Notion returned 404 -- usually the page ID is wrong, or the "
            f"integration hasn't been shared with this page. Detail: {message}"
        )
    return f"Notion API error {resp.status_code}: {message}"


def compute_week_number(start_date, today=None):
    """start_date: date object for the first day of week 1. Returns the
    1-based week number `today` falls in (week 1 = start_date through 6 days
    later, week 2 the next 7 days, and so on). 0 if today is before
    start_date."""
    today = today or date.today()
    if today < start_date:
        return 0
    return ((today - start_date).days // 7) + 1


def _ordinal(n):
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def week_number_to_date_range(week_number, start_date):
    """Inverse of compute_week_number(): the (first_day, last_day) date
    range for the given 1-based week number, using the exact same
    convention (week 1 = start_date through 6 days later, week 2 the next
    7 days, and so on)."""
    first_day = start_date + timedelta(days=(week_number - 1) * 7)
    last_day = first_day + timedelta(days=6)
    return first_day, last_day


def format_week_date_range(week_number, start_date):
    """"Week N" -> "August 23rd-29th" (or "August 30th - September 5th" if
    the week spans two months) -- translates a syllabus's bare week-number
    reference into an actual date range, using the same "week 1 starts on"
    date the rest of the app already uses to compute the current week."""
    first_day, last_day = week_number_to_date_range(week_number, start_date)
    if first_day.month == last_day.month:
        return f"{first_day.strftime('%B')} {_ordinal(first_day.day)}-{_ordinal(last_day.day)}"
    return f"{first_day.strftime('%B')} {_ordinal(first_day.day)} - {last_day.strftime('%B')} {_ordinal(last_day.day)}"


class NotionPageClient:
    def __init__(self, token, page_id):
        self.token = token
        self.page_id = page_id

    def get_title(self):
        """Returns the page's current title as plain text."""
        url = f"{NOTION_API_BASE}/pages/{self.page_id}"
        resp = requests.get(url, headers=_headers(self.token), timeout=15)
        if resp.status_code >= 400:
            raise NotionPageError(_explain_error(resp))
        title_prop = resp.json()["properties"]["title"]["title"]
        return "".join(t.get("plain_text", "") for t in title_prop)

    def set_title(self, new_title):
        """Overwrites the page's title with new_title."""
        url = f"{NOTION_API_BASE}/pages/{self.page_id}"
        payload = {"properties": {"title": {"title": [{"type": "text", "text": {"content": new_title}}]}}}
        resp = requests.patch(url, headers=_headers(self.token), json=payload, timeout=15)
        if resp.status_code >= 400:
            raise NotionPageError(_explain_error(resp))

    def get_week_number(self):
        """Parses the week number out of the current title, or None if the
        title doesn't match TITLE_TEMPLATE's pattern (e.g. never set yet)."""
        match = TITLE_PATTERN.match(self.get_title().strip())
        return int(match.group(1)) if match else None

    def set_week_number(self, week_number):
        """Rewrites the title to TITLE_TEMPLATE with the given week number."""
        self.set_title(TITLE_TEMPLATE.format(week=week_number))

    def sync_week_number(self, start_date, today=None):
        """Computes the current week from start_date and updates the title
        only if it's actually different from what's already there -- avoids
        a needless write every time this is checked (e.g. once per app
        launch). Returns the week number that's now in the title."""
        week_number = compute_week_number(start_date, today=today)
        if self.get_week_number() != week_number:
            self.set_week_number(week_number)
        return week_number

    def _fetch_children(self):
        blocks = []
        url = f"{NOTION_API_BASE}/blocks/{self.page_id}/children"
        params = {"page_size": 100}
        while True:
            resp = requests.get(url, headers=_headers(self.token), params=params, timeout=15)
            if resp.status_code >= 400:
                raise NotionPageError(_explain_error(resp))
            data = resp.json()
            blocks.extend(data["results"])
            if not data.get("has_more"):
                break
            params["start_cursor"] = data["next_cursor"]
        return blocks

    def _archive_block(self, block_id):
        url = f"{NOTION_API_BASE}/blocks/{block_id}"
        resp = requests.patch(url, headers=_headers(self.token), json={"archived": True}, timeout=15)
        if resp.status_code >= 400:
            raise NotionPageError(_explain_error(resp))

    def add_todo(self, text):
        """Adds one new unchecked to-do item at the TOP of the list (above
        every existing item) so the newest thing you add is always what you
        see first.

        Notion's API has no "insert before" or "move block" operation at
        all -- append always adds to the end (or right after a given
        existing block via `after`, never before one). The only way to make
        a new item land literally first is the standard workaround: archive
        every existing to-do block, then recreate all of them (preserving
        text and checked state) in ONE batch call with the new item listed
        first -- Notion creates blocks from a batch in the order given, so
        this reliably puts the new item on top with everything else
        following in its original relative order.

        IMPORTANT: every existing to-do item gets a brand-new block id from
        this (the old ones are archived, not reused) -- any id from a
        list_todos() call before this one is stale immediately after. Call
        list_todos() again before targeting a specific item by id if
        add_todo() might have run since your last read."""
        existing = [b for b in self._fetch_children() if b.get("type") == "to_do"]

        for block in existing:
            self._archive_block(block["id"])

        children_payload = [{
            "type": "to_do",
            "to_do": {"rich_text": [{"type": "text", "text": {"content": text}}], "checked": False},
        }]
        for block in existing:
            to_do = block["to_do"]
            children_payload.append({
                "type": "to_do",
                "to_do": {"rich_text": to_do.get("rich_text", []), "checked": bool(to_do.get("checked"))},
            })

        url = f"{NOTION_API_BASE}/blocks/{self.page_id}/children"
        resp = requests.patch(url, headers=_headers(self.token), json={"children": children_payload}, timeout=15)
        if resp.status_code >= 400:
            raise NotionPageError(_explain_error(resp))

    def list_todos(self):
        """Returns [{"id": str, "text": str, "checked": bool}, ...] for
        every to_do block on the page, in document order -- "id" is what
        update_todo_text() / set_todo_checked() / delete_todo() take to
        target one specific item."""
        items = []
        for block in self._fetch_children():
            if block.get("type") != "to_do":
                continue
            to_do = block["to_do"]
            text = "".join(t.get("plain_text", "") for t in to_do.get("rich_text", []))
            items.append({"id": block["id"], "text": text, "checked": bool(to_do.get("checked"))})
        return items

    def update_todo_text(self, block_id, text):
        """Overwrites one to-do item's text, preserving its checked state."""
        url = f"{NOTION_API_BASE}/blocks/{block_id}"
        resp = requests.get(url, headers=_headers(self.token), timeout=15)
        if resp.status_code >= 400:
            raise NotionPageError(_explain_error(resp))
        checked = bool(resp.json().get("to_do", {}).get("checked"))

        payload = {"to_do": {"rich_text": [{"type": "text", "text": {"content": text}}], "checked": checked}}
        resp = requests.patch(url, headers=_headers(self.token), json=payload, timeout=15)
        if resp.status_code >= 400:
            raise NotionPageError(_explain_error(resp))
        return {"ok": True, "id": block_id, "text": text, "checked": checked}

    def set_todo_checked(self, block_id, checked):
        """Checks or unchecks one to-do item, preserving its text."""
        url = f"{NOTION_API_BASE}/blocks/{block_id}"
        resp = requests.get(url, headers=_headers(self.token), timeout=15)
        if resp.status_code >= 400:
            raise NotionPageError(_explain_error(resp))
        rich_text = resp.json().get("to_do", {}).get("rich_text", [])

        payload = {"to_do": {"rich_text": rich_text, "checked": bool(checked)}}
        resp = requests.patch(url, headers=_headers(self.token), json=payload, timeout=15)
        if resp.status_code >= 400:
            raise NotionPageError(_explain_error(resp))
        text = "".join(t.get("plain_text", "") for t in rich_text)
        return {"ok": True, "id": block_id, "text": text, "checked": bool(checked)}

    def delete_todo(self, block_id):
        """Deletes one to-do item by archiving it -- Notion's actual delete
        mechanism (see notion_client.py's module docstring); it moves to
        Trash and is recoverable from Notion's UI."""
        self._archive_block(block_id)
        return {"ok": True, "id": block_id}
