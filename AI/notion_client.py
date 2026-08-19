# General-purpose Notion API wrapper -- unlike notion_table_client.py (one
# specific table block) and notion_page_client.py (one specific to-do
# page), this can search, read, and write anywhere the integration has been
# shared, for the agent's general Notion tools (see notion_agent.py).
#
# "Delete" (archive_page / archive_block) is real here -- Notion's API has
# no permanent-delete endpoint at all; archiving is what Notion itself calls
# deleting, and it moves the page/block to Trash, recoverable from Notion's
# UI like any other delete. There's no extra soft-delete layer on top of
# that here because Notion's own archiving already IS the recoverable
# behavior a soft-delete would try to add.
#
# SETUP: same integration token as the rest of this project. The
# integration only sees pages/databases it's been explicitly shared with
# (Notion's "..." menu -> "Connections") -- search() only searches within
# that shared set, same as everything else here.

import requests

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Block types the agent can append/update -- all of them store their text
# under block[type]["rich_text"], so one code path handles all of them.
TEXT_BLOCK_TYPES = (
    "paragraph", "heading_1", "heading_2", "heading_3", "to_do",
    "bulleted_list_item", "numbered_list_item", "quote", "toggle", "callout",
)


class NotionClientError(Exception):
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
            "Notion returned 404 -- usually the page/block/database ID is wrong, or the "
            f"integration hasn't been shared with it. Detail: {message}"
        )
    return f"Notion API error {resp.status_code}: {message}"


def _rich_text(text):
    return [{"type": "text", "text": {"content": text}}]


def _block_text(block):
    """Plain concatenated text of a block, or "" for block types that don't
    hold rich_text (dividers, images, child databases/pages, tables, etc.)."""
    block_type = block.get("type")
    body = block.get(block_type, {})
    rich_text = body.get("rich_text")
    if rich_text is None:
        return ""
    return "".join(t.get("plain_text", "") for t in rich_text)


def _flatten_property(prop):
    """Turns one Notion page/database property object into a plain value
    for display -- handles the property types that actually show up in
    practice; anything unrecognized falls back to its raw type name."""
    prop_type = prop.get("type")
    if prop_type == "title":
        return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    if prop_type == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    if prop_type == "select":
        value = prop.get("select")
        return value.get("name") if value else None
    if prop_type == "multi_select":
        return [v.get("name") for v in prop.get("multi_select", [])]
    if prop_type == "date":
        value = prop.get("date")
        return value.get("start") if value else None
    if prop_type == "checkbox":
        return prop.get("checkbox")
    if prop_type == "number":
        return prop.get("number")
    if prop_type == "people":
        return [p.get("name") for p in prop.get("people", [])]
    if prop_type == "url":
        return prop.get("url")
    return f"<{prop_type}>"


class NotionClient:
    def __init__(self, token):
        self.token = token

    def _get(self, path, params=None):
        resp = requests.get(f"{NOTION_API_BASE}{path}", headers=_headers(self.token), params=params, timeout=15)
        if resp.status_code >= 400:
            raise NotionClientError(_explain_error(resp))
        return resp.json()

    def _patch(self, path, payload):
        resp = requests.patch(f"{NOTION_API_BASE}{path}", headers=_headers(self.token), json=payload, timeout=15)
        if resp.status_code >= 400:
            raise NotionClientError(_explain_error(resp))
        return resp.json()

    def _post(self, path, payload):
        resp = requests.post(f"{NOTION_API_BASE}{path}", headers=_headers(self.token), json=payload, timeout=15)
        if resp.status_code >= 400:
            raise NotionClientError(_explain_error(resp))
        return resp.json()

    def search(self, query="", limit=20):
        """Searches every page/database the integration can see. Returns
        [{"id", "type" ("page"/"database"), "title"}, ...].

        Notion's search API is a broad relevance/recency-ranked search, not
        a title filter -- querying "CS311" still returns lots of unrelated
        pages (observed: CS210/CS285/etc. mixed in). When a query is given,
        this filters down to titles that actually contain it (case-
        insensitive) before returning, so the tool is precise rather than
        just relaying Notion's fuzzy ranking. Falls back to the unfiltered
        results if that filter would return nothing, rather than hiding
        everything over an exact-substring miss."""
        payload = {"page_size": min(limit, 100)}
        if query:
            payload["query"] = query
        data = self._post("/search", payload)

        results = []
        for item in data.get("results", []):
            item_type = item.get("object")  # "page" or "database"
            title = ""
            if item_type == "database":
                title = "".join(t.get("plain_text", "") for t in item.get("title", []))
            else:
                for prop in item.get("properties", {}).values():
                    if prop.get("type") == "title":
                        title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                        break
            results.append({
                "id": item.get("id"),
                "type": item_type,
                "title": title,
                "url": item.get("url", ""),
            })

        if query:
            filtered = [r for r in results if query.lower() in r["title"].lower()]
            if filtered:
                return filtered[:limit]
        return results[:limit]

    def get_page_title(self, page_id):
        data = self._get(f"/pages/{page_id}")
        for prop in data.get("properties", {}).values():
            if prop.get("type") == "title":
                return "".join(t.get("plain_text", "") for t in prop.get("title", []))
        return ""

    def get_block_children(self, block_id):
        """Returns the direct children of a page or block, simplified to
        [{"id", "type", "text", "has_children"}, ...] -- one level deep;
        pass a child's id back in to go deeper if has_children is true."""
        children = []
        params = {"page_size": 100}
        path = f"/blocks/{block_id}/children"
        while True:
            data = self._get(path, params=params)
            for block in data.get("results", []):
                children.append({
                    "id": block.get("id"),
                    "type": block.get("type"),
                    "text": _block_text(block),
                    "has_children": bool(block.get("has_children")),
                })
            if not data.get("has_more"):
                break
            params["start_cursor"] = data["next_cursor"]
        return children

    def append_block(self, parent_id, block_type, text):
        """Appends one new text block (see TEXT_BLOCK_TYPES) to the end of
        a page's or block's children."""
        if block_type not in TEXT_BLOCK_TYPES:
            raise NotionClientError(f"Unsupported block_type '{block_type}'. Valid: {', '.join(TEXT_BLOCK_TYPES)}")
        body = {"rich_text": _rich_text(text)}
        if block_type == "to_do":
            body["checked"] = False
        payload = {"children": [{"type": block_type, block_type: body}]}
        result = self._patch(f"/blocks/{parent_id}/children", payload)
        new_block = result["results"][0]
        return {"id": new_block["id"], "type": block_type}

    def update_block_text(self, block_id, text):
        """Overwrites an existing block's text, preserving its type and any
        other state it has (e.g. a to-do's checked state)."""
        block = self._get(f"/blocks/{block_id}")
        block_type = block.get("type")
        if block_type not in TEXT_BLOCK_TYPES:
            raise NotionClientError(
                f"Block {block_id} is a '{block_type}' block, which doesn't hold plain text "
                "that can be overwritten this way."
            )
        body = dict(block.get(block_type, {}))
        body["rich_text"] = _rich_text(text)
        self._patch(f"/blocks/{block_id}", {block_type: body})
        return {"ok": True, "id": block_id, "type": block_type}

    def create_page(self, parent_page_id, title):
        """Creates a new blank sub-page under an existing page."""
        payload = {
            "parent": {"page_id": parent_page_id},
            "properties": {"title": {"title": _rich_text(title)}},
        }
        data = self._post("/pages", payload)
        return {"id": data["id"], "title": title}

    def update_page_title(self, page_id, title):
        page = self._get(f"/pages/{page_id}")
        title_prop_name = next(
            (name for name, prop in page.get("properties", {}).items() if prop.get("type") == "title"),
            "title",
        )
        self._patch(f"/pages/{page_id}", {"properties": {title_prop_name: {"title": _rich_text(title)}}})
        return {"ok": True, "id": page_id, "title": title}

    def query_database(self, database_id, limit=50):
        """Returns [{"id", "properties": {name: flattened_value, ...}}, ...]
        for a database's rows."""
        data = self._post(f"/databases/{database_id}/query", {"page_size": min(limit, 100)})
        rows = []
        for page in data.get("results", []):
            properties = {name: _flatten_property(prop) for name, prop in page.get("properties", {}).items()}
            rows.append({"id": page.get("id"), "properties": properties})
        return rows

    def _calendar_property_names(self, database_id):
        """Auto-detects a calendar-style database's title/date property
        names from its schema, since they vary per database -- shared by
        every calendar method below rather than each re-deriving it."""
        schema = self._get(f"/databases/{database_id}")
        properties_schema = schema.get("properties", {})
        title_property = next(
            (name for name, prop in properties_schema.items() if prop.get("type") == "title"), "Name"
        )
        date_property = next(
            (name for name, prop in properties_schema.items() if prop.get("type") == "date"), "Date"
        )
        return title_property, date_property

    def list_calendar_entries(self, database_id, limit=None):
        """Returns [{"id": str, "date": "YYYY-MM-DD", "title": str}, ...]
        for every row in a calendar-style database -- for avoiding double-
        booking already-busy days when scheduling new study/work sessions
        there (the title is kept, not just the date, so a caller can tell
        what KIND of session already occupies a day via a title-prefix
        convention -- see study_planner.py), and "id" so a specific entry
        can be targeted by update_calendar_entry() / delete_calendar_entry().

        Paginates internally rather than delegating to query_database()
        (which is capped at one page, for the general-purpose agent tool
        where that's intentional) -- callers of this method, including
        NotionAgent.clear_all_data(), depend on genuinely getting every
        row, not just the first 100. Pass limit to cap the result anyway."""
        title_property, date_property = self._calendar_property_names(database_id)
        rows = []
        payload = {"page_size": 100}
        while True:
            data = self._post(f"/databases/{database_id}/query", payload)
            rows.extend(data.get("results", []))
            if not data.get("has_more") or (limit is not None and len(rows) >= limit):
                break
            payload["start_cursor"] = data["next_cursor"]
        if limit is not None:
            rows = rows[:limit]

        entries = []
        for row in rows:
            properties = {name: _flatten_property(prop) for name, prop in row.get("properties", {}).items()}
            date_value = properties.get(date_property)
            if date_value:
                entries.append({
                    "id": row.get("id"),
                    "date": date_value,
                    "title": properties.get(title_property) or "",
                })
        return entries

    def clear_calendar(self, database_id):
        """Archives every entry in a calendar-style database. Returns how
        many were archived."""
        entries = self.list_calendar_entries(database_id)
        for entry in entries:
            self.delete_calendar_entry(entry["id"])
        return len(entries)

    def add_calendar_entry(self, database_id, title, date_str):
        """Creates a new row in a calendar-style database (one title
        property + one date property, e.g. a "To Do Date" calendar). The
        title/date property names are auto-detected from the database's
        schema rather than assumed, since they vary per database."""
        title_property, date_property = self._calendar_property_names(database_id)
        payload = {
            "parent": {"database_id": database_id},
            "properties": {
                title_property: {"title": _rich_text(title)},
                date_property: {"date": {"start": date_str}},
            },
        }
        data = self._post("/pages", payload)
        return {"id": data["id"], "title": title, "date": date_str}

    def update_calendar_entry(self, database_id, page_id, title=None, date_str=None):
        """Updates an existing calendar row's title and/or date -- pass only
        the one(s) you want to change. database_id is needed to look up the
        title/date property names for this database (they vary per
        database, same as add_calendar_entry())."""
        title_property, date_property = self._calendar_property_names(database_id)
        properties = {}
        if title is not None:
            properties[title_property] = {"title": _rich_text(title)}
        if date_str is not None:
            properties[date_property] = {"date": {"start": date_str}}
        if not properties:
            raise NotionClientError("update_calendar_entry: give at least a title or a date to change.")
        self._patch(f"/pages/{page_id}", {"properties": properties})
        return {"ok": True, "id": page_id, "title": title, "date": date_str}

    def delete_calendar_entry(self, page_id):
        """Deletes (archives -- see module docstring) a calendar row."""
        return self.archive_page(page_id)

    def archive_page(self, page_id):
        """Deletes a page by archiving it -- Notion's actual delete
        mechanism; the page moves to Trash and is recoverable from Notion's
        UI, same as deleting it by hand would be. Works for a database row
        (a row IS a page) as well as a regular page."""
        self._patch(f"/pages/{page_id}", {"archived": True})
        return {"ok": True, "id": page_id}

    def archive_block(self, block_id):
        """Deletes a block (e.g. a to-do item, a paragraph) by archiving
        it -- same Trash-and-recoverable semantics as archive_page()."""
        self._patch(f"/blocks/{block_id}", {"archived": True})
        return {"ok": True, "id": block_id}
