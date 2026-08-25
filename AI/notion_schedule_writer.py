# Writes schedule entries (the kind llm_client.extract_schedule() returns)
# into a Notion database as new pages/rows.
#
# THIS IS A STANDALONE FIRST DRAFT: I don't have your existing
# services/notion_api.py yet, so this is written independently rather than
# merged into it. Once you share that file, this can likely be folded in (or
# just imported from it) instead of duplicating auth/config handling.
#
# SETUP:
#   1. Create an integration at https://www.notion.so/my-integrations
#      ("Internal" type), copy its token (starts with "secret_")
#   2. Open your target database in Notion -> "..." menu -> "Connections" ->
#      add your integration (it can't see the database otherwise, even with
#      a valid token)
#   3. Copy the database ID from its URL:
#      notion.so/workspace/DATABASE_ID?v=... (the 32-char hex string right
#      before the "?")
#
# PLACEHOLDER SCHEMA: this assumes your database has a Title property, a Date
# property, and a Select property for the event type -- EDIT
# NAME_PROPERTY/DATE_PROPERTY/TYPE_PROPERTY below to match your actual
# database if the names differ. (Every Notion database has exactly one Title
# property, whatever it's actually named -- that's the one NAME_PROPERTY
# needs to point at.)

import requests

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"  # widely-supported pinned version; bump if you need newer features

# ---- EDIT THESE to match your actual Notion database ----
NAME_PROPERTY = "Name"
DATE_PROPERTY = "Date"
TYPE_PROPERTY = "Type"


class NotionWriteError(Exception):
    pass


class NotionScheduleWriter:
    def __init__(self, token, database_id):
        self.token = token
        self.database_id = database_id
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def add_entry(self, date_str, title, entry_type="other", time_str=None):
        """date_str: 'YYYY-MM-DD'. Returns the created page's Notion URL."""
        notion_date = f"{date_str}T{time_str}:00" if time_str else date_str

        properties = {
            NAME_PROPERTY: {"title": [{"text": {"content": title}}]},
            DATE_PROPERTY: {"date": {"start": notion_date}},
            TYPE_PROPERTY: {"select": {"name": entry_type}},
        }
        payload = {
            "parent": {"database_id": self.database_id},
            "properties": properties,
        }

        resp = requests.post(f"{NOTION_API_BASE}/pages", headers=self.headers, json=payload, timeout=15)
        if resp.status_code >= 400:
            raise NotionWriteError(_explain_error(resp))
        return resp.json().get("url")

    def add_entries(self, entries):
        """entries: list of {"date","title","type","time"} dicts (the shape
        llm_client.extract_schedule() returns). Returns (successes, failures)
        -- failures is a list of (entry, error_message) so one bad entry
        doesn't lose the rest of the batch."""
        successes, failures = [], []
        for entry in entries:
            try:
                url = self.add_entry(
                    date_str=entry["date"],
                    title=entry["title"],
                    entry_type=entry.get("type") or "other",
                    time_str=entry.get("time"),
                )
                successes.append((entry, url))
            except NotionWriteError as e:
                failures.append((entry, str(e)))
        return successes, failures


def _explain_error(resp):
    try:
        message = resp.json().get("message", resp.text)
    except ValueError:
        message = resp.text
    if resp.status_code == 401:
        return f"Notion rejected the token (401) -- check it's correct and hasn't expired. Detail: {message}"
    if resp.status_code == 404:
        return (
            "Notion returned 404 -- usually means the database ID is wrong, or the "
            f"integration hasn't been given access via 'Connections' in Notion. Detail: {message}"
        )
    if resp.status_code == 400:
        return (
            "Notion rejected the request (400) -- almost always a property name/type "
            f"mismatch. Check NAME_PROPERTY/DATE_PROPERTY/TYPE_PROPERTY match your database. Detail: {message}"
        )
    return f"Notion API error {resp.status_code}: {message}"
