# Reads and writes a Notion TABLE BLOCK (a plain grid inserted with /table),
# NOT a database. These are different Notion API surfaces -- a table block
# holds no data itself; each visible row is a separate table_row child block,
# and there's no "update just this one cell" endpoint: PATCHing a row
# replaces its ENTIRE cells array at once. So writing one cell always means:
# fetch that row's current cells, change just the one you want, PATCH the
# whole row back -- update_cell() below does this for you.
#
# GETTING THE TABLE'S BLOCK ID: in Notion, hover the table, click the
# six-dot drag handle (or right-click a cell) -> "Copy link to block". The
# URL looks like notion.so/yourpage-abc123...#xyz789... -- the part after
# the "#" is the table's block ID.
#
# SETUP: same integration token as the rest of this project (see
# notion_schedule_writer.py's header) -- the integration also needs to be
# shared with the PAGE that contains this table (Notion's "Connections" menu),
# not just a database.
#
# Row labels are READ FROM THE REAL TABLE, not assumed from a fixed list --
# the only structural assumption made here is that row 0 is always the
# course-name row (true in every version of the table seen so far). Every
# other row is whatever the real table actually has, in real order,
# discovered fresh on every get_state() call. This is deliberate: an
# earlier version hardcoded an expected ROW_LABELS list and validated the
# real table's row COUNT against it, which broke (twice) the moment the
# real table gained a row -- a hardcoded expectation of someone else's
# living document is exactly the kind of assumption that goes stale.
#
# ROW_LABELS below is kept only as a REFERENCE of the fields known about as
# of this writing (e.g. for study_planner.WORK_FIELD_LABELS to classify
# against) -- it is NOT used to validate or reject the real table anymore.
# Column 0 of every row is the label (never touched); columns 1..N are one
# per course -- however many the table actually has, read from the real
# table rather than hardcoded.

import requests

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

ROW_LABELS = [
    "Course:", "Quiz-1", "Quiz-2", "Quiz-3", "Quiz-4", "Quiz-5",
    "Assignments", "Assignments-2", "Certificate or Coursera",
    "Project", "Project-2", "Major-1", "Major-2", "Final",
]
COURSE_NAME_ROW = 0  # the one structural assumption -- row 0 always holds each column's course name


class NotionTableError(Exception):
    pass


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _cell_text(cell):
    """cell: a list of Notion rich-text objects (one Notion cell). Returns the
    plain concatenated text, e.g. "" if empty, "TBA" if that's what's there."""
    parts = []
    for piece in cell:
        parts.append(piece.get("plain_text") or piece.get("text", {}).get("content", ""))
    return "".join(parts).strip()


def _text_cell(text):
    """Build a single Notion cell (a list with one rich-text run) from a plain string."""
    return [{"type": "text", "text": {"content": text}}]


def _explain_error(resp):
    try:
        message = resp.json().get("message", resp.text)
    except ValueError:
        message = resp.text
    if resp.status_code == 401:
        return f"Notion rejected the token (401). Detail: {message}"
    if resp.status_code == 404:
        return (
            "Notion returned 404 -- usually the block ID is wrong, or the integration "
            f"hasn't been shared with the page this table lives on. Detail: {message}"
        )
    return f"Notion API error {resp.status_code}: {message}"


class TableState:
    """Snapshot of the table's current contents plus derived "what's missing" info.
    grid[row_index][col_index] -> plain text (col_index is 0-based among course
    columns only, i.e. NOT counting the label column). row_labels holds the
    REAL label text read from each row's own first cell, in real document
    order -- discovered fresh every read, not assumed from a fixed list."""

    def __init__(self, row_ids, row_labels, num_columns, grid):
        self.row_ids = row_ids
        self.row_labels = row_labels
        self.num_columns = num_columns
        self.grid = grid

    @staticmethod
    def is_filled(text):
        """"" is missing. "TBA" / "N/A" / an actual date are all valid, filled answers --
        there's no special-casing needed, any non-blank text counts."""
        return bool(text and text.strip())

    def course_name(self, col):
        return self.grid[COURSE_NAME_ROW][col]

    def field_names(self):
        """Every field name EXCEPT the course-name row, in real table order."""
        return [label for i, label in enumerate(self.row_labels) if i != COURSE_NAME_ROW]

    def row_index_for_field(self, field_name):
        """Case-insensitive lookup of a row by its REAL label text (as
        currently shown in Notion). Returns None if no row has that label
        -- e.g. it was renamed or removed since this state was read."""
        target = field_name.strip().lower()
        for i, label in enumerate(self.row_labels):
            if label.strip().lower() == target:
                return i
        return None

    def missing_rows_for_column(self, col):
        """Row indices that are still empty for this column, including the
        course-name row itself if that's blank."""
        return [r for r in range(len(self.row_labels)) if not self.is_filled(self.grid[r][col])]

    def is_column_complete(self, col):
        return len(self.missing_rows_for_column(col)) == 0

    def first_incomplete_column(self):
        """Returns the first column index that still has something missing,
        or None if the whole table is filled in."""
        for col in range(self.num_columns):
            if not self.is_column_complete(col):
                return col
        return None

    def is_complete(self):
        return self.first_incomplete_column() is None

    def find_column_by_course_name(self, name):
        """Case-insensitive match against already-entered course names, for
        matching a scanned syllabus to an existing (partially-filled) column
        rather than assuming the next empty column."""
        name = name.strip().lower()
        for col in range(self.num_columns):
            existing = self.course_name(col).strip().lower()
            if existing and existing == name:
                return col
        return None

    def first_empty_course_column(self):
        """First column with no course name at all yet -- where a newly
        scanned syllabus should land if its course name doesn't match anything
        already in the table."""
        for col in range(self.num_columns):
            if not self.is_filled(self.course_name(col)):
                return col
        return None


class NotionTableClient:
    def __init__(self, token, table_block_id):
        self.token = token
        self.table_block_id = table_block_id

    def fetch_rows(self):
        """Returns the raw list of table_row blocks, in order, straight from
        Notion (each has an 'id' and 'table_row': {'cells': [...]})."""
        rows = []
        url = f"{NOTION_API_BASE}/blocks/{self.table_block_id}/children"
        params = {"page_size": 100}
        while True:
            resp = requests.get(url, headers=_headers(self.token), params=params, timeout=15)
            if resp.status_code >= 400:
                raise NotionTableError(_explain_error(resp))
            data = resp.json()
            rows.extend(b for b in data["results"] if b.get("type") == "table_row")
            if not data.get("has_more"):
                break
            params["start_cursor"] = data["next_cursor"]
        return rows

    def get_state(self):
        """Returns a TableState describing the current grid contents and
        which cells are still missing. Row labels come straight from each
        row's own first cell -- reads whatever the real table actually has,
        however many rows, in whatever order, no fixed count expected."""
        rows = self.fetch_rows()
        if not rows:
            raise NotionTableError("The table has no rows at all -- nothing to read.")

        row_labels = [_cell_text(row["table_row"]["cells"][0]) for row in rows]
        num_columns = len(rows[0]["table_row"]["cells"]) - 1  # minus the label column
        grid = [[_cell_text(c) for c in row["table_row"]["cells"][1:]] for row in rows]

        return TableState(
            row_ids=[r["id"] for r in rows], row_labels=row_labels, num_columns=num_columns, grid=grid,
        )

    def clear_all(self):
        """Blanks every cell in the table -- including course names --
        across every row and column, restoring it to an empty template
        while leaving the row/column structure exactly as it is. Skips
        cells that are already blank (no wasted writes). Returns how many
        cells were actually cleared."""
        state = self.get_state()
        cleared = 0
        for row in range(len(state.row_labels)):
            for col in range(state.num_columns):
                if state.grid[row][col]:
                    self.update_cell(row, col, "")
                    cleared += 1
        return cleared

    def update_cell(self, row_index, column_index, text):
        """row_index: a real row index, e.g. from
        TableState.row_index_for_field(). column_index: 0-based among
        course columns (not counting the label column). Does a
        read-modify-write since Notion has no single-cell PATCH."""
        rows = self.fetch_rows()
        if row_index >= len(rows):
            raise NotionTableError(f"Row index {row_index} out of range (table has {len(rows)} rows)")

        row_block = rows[row_index]
        cells = row_block["table_row"]["cells"]
        target_cell_index = column_index + 1  # +1 to skip the label column
        if target_cell_index >= len(cells):
            raise NotionTableError(
                f"Column index {column_index} out of range (table has {len(cells) - 1} course columns)"
            )

        new_cells = list(cells)
        new_cells[target_cell_index] = _text_cell(text)

        url = f"{NOTION_API_BASE}/blocks/{row_block['id']}"
        payload = {"table_row": {"cells": new_cells}}
        resp = requests.patch(url, headers=_headers(self.token), json=payload, timeout=15)
        if resp.status_code >= 400:
            raise NotionTableError(_explain_error(resp))
