# The agent IS the LLM hosted in LM Studio: every read/write/update/delete
# happens because the model itself decided to call a tool, not because
# Python parsed intent or matched text against rows. Python's only job is
# executing whatever tool call the model makes and handing the result back
# -- see _dispatch_tool_call(). A pasted screenshot is just part of the
# conversation (see chat()'s images_b64 param) -- built as a multimodal
# message the SAME model sees alongside its tools, not relayed through a
# separate vision-only model. That only works if the model you have loaded
# in LM Studio actually supports vision + tool calling together; if it
# doesn't, tool calls will silently stop happening on image turns even
# though the reply sounds confident -- see ScheduleChatPanel's live status
# line (shows the model + which tool, if any, actually got called) and the
# on_tool_result success/failure lines in the chat log if that happens.
#
# General-purpose by design, not scoped to "the schedule": three
# domain-specific tool groups (exam table, to-do list, calendar) that
# understand the actual shape of those Notion objects, PLUS general
# search/read/create/update/delete tools (via notion_client.NotionClient)
# for anything else the integration can see. Full read/write/update/delete
# is intentional here -- "delete" means Notion's own archive-to-Trash
# mechanism (there's no other kind of delete in Notion's API), which is
# recoverable from Notion's UI like any other delete, not a permanent nuke.

import re
from datetime import date, datetime

from llm_client import LLMClient
from notion_table_client import NotionTableClient, COURSE_NAME_ROW
from notion_client import NotionClient


class NotionAgentError(Exception):
    pass


def _ordinal(n):
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def humanize_date(value):
    """Turns a normalized 'YYYY-MM-DD[ HH:MM]' value into human-readable
    text like "April 18th" (or "April 18th at 2:00 PM" with a time), for
    display in the Notion table. TBA/N-A/empty and anything else that
    doesn't match the expected shape pass straight through unchanged (an
    empty string stays empty -- that's how a cell gets cleared)."""
    if not value:
        return value

    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?: (\d{2}):(\d{2}))?$", value.strip())
    if not match:
        return value

    year, month, day, hour, minute = match.groups()
    text = f"{datetime(int(year), int(month), int(day)).strftime('%B')} {_ordinal(int(day))}"
    if hour is not None:
        time_text = datetime(2000, 1, 1, int(hour), int(minute)).strftime("%I:%M %p").lstrip("0")
        text += f" at {time_text}"
    return text




CHAT_SYSTEM_PROMPT = """You are Study Warden's assistant. Today's date is {today}. You're a general \
assistant first -- answer whatever the user asks, related to their coursework or not, the same way any \
capable assistant would. On top of that, you have real tools to read and change three specific things \
in the user's Notion workspace (an exam/assignment table, a to-do list, a calendar), plus general tools \
for anything else in their Notion workspace. You have no other way to see or change any of them, so if \
the user gives you new information or asks about current state, actually call a tool rather than just \
saying you will or describing what you'd do.

When you already know several separate calls you need to make (e.g. several exam-table fields from one \
image, or several to-do items at once), make ALL of those tool calls together in the SAME response \
rather than one at a time across several responses -- you can return more than one tool call at once. \
There's a hard limit on how many back-and-forth exchanges one conversation turn gets; batching what you \
already know into fewer, fuller responses avoids running out partway through a multi-step task.

You are fully permitted to read, write, update, AND delete -- nothing is off-limits by default. \
"Delete" (delete_todo, delete_calendar_entry, notion_delete) archives the item to Notion's Trash, which \
is exactly what deleting something in Notion normally does -- it's recoverable from Notion's UI, not a \
permanent, unrecoverable action, so there's no need to hedge or ask for extra confirmation before doing \
it when the user has clearly asked for it.

EXAM TABLE: one column per course, with these fields: {exam_fields}. "Major-1"/"Major-2" mean the \
first/second MIDTERM exam -- "major" and "midterm" are the same thing here, just use the field's real \
name ("Major-1"/"Major-2") when calling update_exam_cell.

Column indexes are NOT something you already know or can guess -- always call get_exam_table first if \
you don't already know which column a course is in from earlier in this conversation. Never invent a \
column number, and never derive it from a field's position in the list above -- column identifies the \
COURSE, field identifies the ROW, they are unrelated numbers.

Example: get_exam_table returns columns: [{{"column": 0, "course_name": "CS223", ...}}]. The user says \
"CS223 major 1 is October 15th". You call update_exam_cell(column=0, field="Major-1", \
value="{this_year}-10-15") -- column is 0 because that's CS223's column, NOT because "Major-1" is the \
9th field in the list.

For update_exam_cell's value: write a specific date as YYYY-MM-DD (assume {this_year} if no year is \
given) -- optionally add " HH:MM" in 24-hour time. It gets reformatted for display automatically. Use \
"TBA" if the date isn't announced yet, "N/A" if that field doesn't apply to this course, or an empty \
string "" to clear a field back to blank.

IMAGES: the user may attach a screenshot (e.g. a syllabus grading table) directly in the conversation --\
 look at it yourself and act on what it shows, the same as you would for typed text. If it clearly gives \
dates for named graded items (quizzes, exams, midterms, finals, projects, assignments), actually call \
update_exam_cell for each one right now -- do not just summarize or describe what you saw back to the \
user instead of calling the tool. If the image doesn't make the course clear (no course name/code \
visible anywhere in it, and none given earlier in this conversation), ASK the user which course it's for \
before writing anything -- don't guess. If a row only gives an indirect reference (a week number, \
session number, etc.) with no explicit date anywhere to resolve it from, leave that item out and ask \
rather than guessing a date -- a missing item is far better than a wrong one. If an item gives several \
possible dates (e.g. "6th, 7th, or 19th Sep as Feasible"), use the earliest one.

Example: an image shows a grading table with "Quiz 1 - 5% - Sep 6th", "Midterm Exam - 15% - Oct 26 \
2026", "Final Exam - 40% - date TBA", and no course name is visible anywhere. You ask the user what \
course this is for. They say "CS223". You call get_exam_table, and once you know CS223's column N, you \
call update_exam_cell(column=N, field="Quiz-1", value="{this_year}-09-06"), \
update_exam_cell(column=N, field="Major-1", value="2026-10-26"), and \
update_exam_cell(column=N, field="Final", value="TBA") ALL TOGETHER in that one response -- three real \
tool calls in the same turn, not a description of them and not three separate back-and-forth turns.

TO-DO LIST: list_todos / add_todo / update_todo / set_todo_checked / delete_todo. Call add_todo when the \
user asks you to remember/add/remind them of something that isn't exam-table or calendar data -- new \
items are added at the TOP of the list, so it always shows the newest thing first.

When the user says they've done something, finished something, or asks you to check/cross/tick something \
off (e.g. "I emailed the professor", "mark buy a mouse as done", "cross off the library book") -- call \
set_todo_checked(todo_id, checked=true) for the matching item, the same as clicking its checkbox in \
Notion yourself. Don't just acknowledge it in text without actually calling the tool.

IMPORTANT: add_todo() gives every OTHER existing to-do item a brand-new id (it has to recreate them to \
put the new one on top -- see its own description). Any todo_id you learned from a list_todos() call \
before an add_todo() happened is stale. If you're not sure your list is current -- especially right \
after calling add_todo, or if the user refers to an item you haven't listed yet this conversation -- \
call list_todos() again before calling update_todo / set_todo_checked / delete_todo with an id.

CALENDAR: list_calendar_entries / add_calendar_entry / update_calendar_entry / delete_calendar_entry -- \
a date-based schedule of study/work sessions and other events, separate from the exam table (which holds \
due dates) and the to-do list (which holds plain checklist items).

GENERAL NOTION: notion_search / notion_read_page / notion_append_content / notion_update_block_text / \
notion_create_page / notion_update_page_title / notion_query_database / notion_delete -- for anything \
in the user's Notion workspace that isn't the exam table, to-do list, or calendar specifically. Search \
first if you don't already know a page's ID.

If a tool call's result includes an "error", the action did NOT happen -- tell the user plainly that it \
failed and why, never claim it succeeded. If the error tells you how to fix the call (e.g. "call \
get_exam_table"), do that and retry once with corrected arguments; don't repeat the exact same failing \
call more than once.

Be conversational and concise -- a sentence or two is usually enough. After calling tools, tell the user \
plainly what you did or what you found, don't just restate the raw tool output."""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_exam_table",
            "description": (
                "Reads the current exam/assignment table from Notion: every course column's name, "
                "every field's current value, and which fields are still blank."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_exam_cell",
            "description": (
                "Writes (or clears, with an empty value) one field in the exam table for one course "
                "column. Use field 'course_name' to set/change the course's name; use an exact row "
                "name for anything else, e.g. 'Quiz-3', 'Major-1' (the midterm), 'Final', 'Project', "
                "'Assignments'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "column": {
                        "type": "integer",
                        "description": (
                            "Which COURSE this is, as the 0-based 'column' index from get_exam_table's "
                            "'columns' list. Unrelated to 'field' or a row's position in any list."
                        ),
                    },
                    "field": {
                        "type": "string",
                        "description": "'course_name' or an exact row name like 'Quiz-3'.",
                    },
                    "value": {
                        "type": "string",
                        "description": "A date (YYYY-MM-DD[ HH:MM]), 'TBA', 'N/A', the course name, or '' to clear the field.",
                    },
                },
                "required": ["column", "field", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_todos",
            "description": "Reads the user's current to-do list items: id, text, and whether each is checked off.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_todo",
            "description": "Adds one new item to the user's to-do list.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "The to-do item's text."}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_todo",
            "description": "Changes an existing to-do item's text. Call list_todos first if you don't already know its id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "todo_id": {"type": "string", "description": "The to-do item's id, from list_todos."},
                    "text": {"type": "string", "description": "The new text."},
                },
                "required": ["todo_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_todo_checked",
            "description": "Checks or unchecks an existing to-do item. Call list_todos first if you don't already know its id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "todo_id": {"type": "string", "description": "The to-do item's id, from list_todos."},
                    "checked": {"type": "boolean", "description": "True to check it off, false to uncheck it."},
                },
                "required": ["todo_id", "checked"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_todo",
            "description": "Deletes (archives) an existing to-do item. Call list_todos first if you don't already know its id.",
            "parameters": {
                "type": "object",
                "properties": {"todo_id": {"type": "string", "description": "The to-do item's id, from list_todos."}},
                "required": ["todo_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_calendar_entries",
            "description": "Reads every entry on the user's calendar: id, date, and title.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_calendar_entry",
            "description": "Adds a new entry to the user's calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "The entry's title."},
                    "date": {"type": "string", "description": "YYYY-MM-DD."},
                },
                "required": ["title", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_calendar_entry",
            "description": "Changes an existing calendar entry's title and/or date. Call list_calendar_entries first if you don't already know its id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string", "description": "The entry's id, from list_calendar_entries."},
                    "title": {"type": "string", "description": "New title (omit to leave unchanged)."},
                    "date": {"type": "string", "description": "New date, YYYY-MM-DD (omit to leave unchanged)."},
                },
                "required": ["entry_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_entry",
            "description": "Deletes (archives) an existing calendar entry. Call list_calendar_entries first if you don't already know its id.",
            "parameters": {
                "type": "object",
                "properties": {"entry_id": {"type": "string", "description": "The entry's id, from list_calendar_entries."}},
                "required": ["entry_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notion_search",
            "description": "Searches every page/database in the user's Notion workspace the integration can see. Use before notion_read_page/notion_query_database if you don't already know a page's id.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search text, or empty for a broad listing."}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notion_read_page",
            "description": "Reads a page's title and its content blocks (one level deep).",
            "parameters": {
                "type": "object",
                "properties": {"page_id": {"type": "string", "description": "The page's id, from notion_search."}},
                "required": ["page_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notion_append_content",
            "description": "Appends one new text block to the end of a page's or block's content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "parent_id": {"type": "string", "description": "The page or block id to append under."},
                    "block_type": {
                        "type": "string",
                        "description": "paragraph, heading_1, heading_2, heading_3, to_do, bulleted_list_item, numbered_list_item, quote, toggle, or callout.",
                    },
                    "text": {"type": "string", "description": "The block's text."},
                },
                "required": ["parent_id", "block_type", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notion_update_block_text",
            "description": "Overwrites an existing text block's content, keeping its type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "block_id": {"type": "string", "description": "The block's id."},
                    "text": {"type": "string", "description": "The new text."},
                },
                "required": ["block_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notion_create_page",
            "description": "Creates a new blank sub-page under an existing page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "parent_page_id": {"type": "string", "description": "The page id to create the new page under."},
                    "title": {"type": "string", "description": "The new page's title."},
                },
                "required": ["parent_page_id", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notion_update_page_title",
            "description": "Renames an existing page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "The page's id."},
                    "title": {"type": "string", "description": "The new title."},
                },
                "required": ["page_id", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notion_query_database",
            "description": "Reads every row of a Notion database (not the exam table or calendar -- use their dedicated tools for those) with its properties.",
            "parameters": {
                "type": "object",
                "properties": {"database_id": {"type": "string", "description": "The database's id, from notion_search."}},
                "required": ["database_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notion_delete",
            "description": "Deletes (archives) any page or block by id -- for anything outside the exam table/to-do list/calendar, which have their own dedicated delete tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The page or block id to delete."},
                    "kind": {"type": "string", "description": "'page' or 'block'."},
                },
                "required": ["id", "kind"],
            },
        },
    },
]


class NotionAgent:
    def __init__(self, notion_token, table_block_id, calendar_database_id=None, todo_page=None, llm_client=None):
        self.exam_table = NotionTableClient(notion_token, table_block_id)
        self.notion = NotionClient(notion_token)
        self.calendar_database_id = calendar_database_id
        self.todo_page = todo_page  # optional NotionPageClient for the to-do list page
        self.llm = llm_client or LLMClient()
        self._state = None

    def refresh(self):
        """Re-reads the exam table from Notion. Call at startup and after
        every write, so the agent always works off real current state."""
        self._state = self.exam_table.get_state()
        return self._state

    def clear_all_data(self):
        """Wipes every cell in the exam table (course names + every field,
        for every column), archives every entry in the study calendar, and
        archives every item in the to-do list -- restores all three to an
        empty starting state. Doesn't touch saved connection settings or
        anything else in the user's Notion workspace. "Delete" here is
        Notion's own archive-to-Trash (see notion_client.py's module
        docstring), so this is recoverable from Notion's UI, same as any
        other delete in this app. Returns {"table_cells_cleared": int,
        "calendar_entries_cleared": int, "todo_items_cleared": int}."""
        table_cleared = self.exam_table.clear_all()
        self.refresh()

        calendar_cleared = 0
        if self.calendar_database_id:
            calendar_cleared = self.notion.clear_calendar(self.calendar_database_id)

        todo_cleared = 0
        if self.todo_page is not None:
            for item in self.todo_page.list_todos():
                self.todo_page.delete_todo(item["id"])
                todo_cleared += 1

        return {
            "table_cells_cleared": table_cleared,
            "calendar_entries_cleared": calendar_cleared,
            "todo_items_cleared": todo_cleared,
        }

    @property
    def state(self):
        if self._state is None:
            self.refresh()
        return self._state

    def chat(self, user_text, history=None, images_b64=None, on_progress=None, on_tool_result=None):
        """One turn of a real multi-turn conversation -- the LLM decides
        for itself, via tool calls, what (if anything) to read or write.
        history: the messages list returned by a previous chat() call, or
        None to start a fresh conversation. images_b64: optional image(s)
        attached to this turn (e.g. a pasted screenshot) -- built into the
        SAME multimodal message the model sees alongside its tools (see
        module docstring for why: one model doing vision + tools together,
        not a relay to a separate vision-only model). on_progress /
        on_tool_result: see LLMClient.chat_with_tools(). Returns
        (reply_text, updated_history)."""
        if history:
            messages = list(history)
        else:
            today = date.today()
            # Read the real, current table fields fresh for every new
            # conversation rather than trusting a list baked in at import
            # time -- the real table's rows have already changed more than
            # once. Best-effort: if Notion can't be reached right now, fall
            # back to a generic description rather than blocking the whole
            # agent (including unrelated questions) on that one read.
            try:
                exam_fields = ", ".join(["course_name"] + self.refresh().field_names())
            except Exception:
                exam_fields = "course_name, and whatever fields get_exam_table returns (call it to see them)"
            system_prompt = CHAT_SYSTEM_PROMPT.format(
                today=today.isoformat(), this_year=today.year, exam_fields=exam_fields,
            )
            messages = [{"role": "system", "content": system_prompt}]

        if images_b64:
            content = [{"type": "text", "text": user_text or "What's in this image?"}]
            for b64 in images_b64:
                content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": user_text})

        reply, updated_messages = self.llm.chat_with_tools(
            messages, TOOLS, self._dispatch_tool_call, on_progress=on_progress, on_tool_result=on_tool_result,
        )
        return reply, updated_messages

    def _dispatch_tool_call(self, name, arguments):
        """Executes one tool call the model made and returns a JSON-
        serializable result. Never raises -- callers feed the result
        straight back to the model, so errors are reported as data (e.g.
        {"error": ...}) rather than blowing up the conversation."""
        handler = self._TOOL_HANDLERS.get(name)
        if handler is None:
            return {"error": f"Unknown tool '{name}'"}
        try:
            return handler(self, arguments)
        except Exception as e:
            return {"error": str(e)}

    # ---- exam table ----

    def _tool_get_exam_table(self, arguments):
        state = self.refresh()
        columns = []
        for col in range(state.num_columns):
            cells = {label: state.grid[r][col] for r, label in enumerate(state.row_labels) if r != COURSE_NAME_ROW}
            missing = [state.row_labels[r] for r in state.missing_rows_for_column(col)]
            columns.append({
                "column": col,
                "course_name": state.course_name(col),
                "cells": cells,
                "missing": missing,
            })
        return {"columns": columns}

    def _tool_update_exam_cell(self, arguments):
        column = arguments.get("column")
        field = str(arguments.get("field", "")).strip()
        raw_value = arguments.get("value")
        if column is None or not field or raw_value is None:
            return {"error": "column, field, and value are all required (value may be '' to clear the cell)."}
        raw_value = str(raw_value).strip()

        try:
            column = int(column)
        except (TypeError, ValueError):
            return {"error": f"column must be an integer, got {column!r}. Call get_exam_table to see valid columns."}

        state = self.state
        if column < 0 or column >= state.num_columns:
            return {
                "error": f"Column {column} doesn't exist -- this table only has columns 0 to "
                         f"{state.num_columns - 1}. Call get_exam_table to see the real column "
                         "indexes and course names before trying again."
            }

        if field.lower() == "course_name":
            row = COURSE_NAME_ROW
            value = raw_value
        else:
            row = state.row_index_for_field(field)
            if row is None or row == COURSE_NAME_ROW:
                return {
                    "error": f"Unknown field '{field}'. Valid fields: course_name, "
                             f"{', '.join(state.field_names())}"
                }
            value = humanize_date(raw_value)

        field_label = state.row_labels[row]
        self.exam_table.update_cell(row, column, value)
        self.refresh()
        return {"ok": True, "column": column, "field": field_label, "value": value}

    # ---- to-do list ----

    def _tool_list_todos(self, arguments):
        if self.todo_page is None:
            return {"error": "No to-do list page is configured."}
        return {"items": self.todo_page.list_todos()}

    def _tool_add_todo(self, arguments):
        if self.todo_page is None:
            return {"error": "No to-do list page is configured."}
        text = str(arguments.get("text", "")).strip()
        if not text:
            return {"error": "text is required."}
        self.todo_page.add_todo(text)
        return {"ok": True, "text": text}

    def _tool_update_todo(self, arguments):
        if self.todo_page is None:
            return {"error": "No to-do list page is configured."}
        todo_id = str(arguments.get("todo_id", "")).strip()
        text = str(arguments.get("text", "")).strip()
        if not todo_id or not text:
            return {"error": "todo_id and text are both required."}
        return self.todo_page.update_todo_text(todo_id, text)

    def _tool_set_todo_checked(self, arguments):
        if self.todo_page is None:
            return {"error": "No to-do list page is configured."}
        todo_id = str(arguments.get("todo_id", "")).strip()
        checked = arguments.get("checked")
        if not todo_id or checked is None:
            return {"error": "todo_id and checked are both required."}
        return self.todo_page.set_todo_checked(todo_id, bool(checked))

    def _tool_delete_todo(self, arguments):
        if self.todo_page is None:
            return {"error": "No to-do list page is configured."}
        todo_id = str(arguments.get("todo_id", "")).strip()
        if not todo_id:
            return {"error": "todo_id is required."}
        return self.todo_page.delete_todo(todo_id)

    # ---- calendar ----

    def _tool_list_calendar_entries(self, arguments):
        if not self.calendar_database_id:
            return {"error": "No calendar is configured."}
        return {"entries": self.notion.list_calendar_entries(self.calendar_database_id)}

    def _tool_add_calendar_entry(self, arguments):
        if not self.calendar_database_id:
            return {"error": "No calendar is configured."}
        title = str(arguments.get("title", "")).strip()
        entry_date = str(arguments.get("date", "")).strip()
        if not title or not entry_date:
            return {"error": "title and date are both required."}
        return self.notion.add_calendar_entry(self.calendar_database_id, title, entry_date)

    def _tool_update_calendar_entry(self, arguments):
        if not self.calendar_database_id:
            return {"error": "No calendar is configured."}
        entry_id = str(arguments.get("entry_id", "")).strip()
        if not entry_id:
            return {"error": "entry_id is required."}
        title = arguments.get("title")
        entry_date = arguments.get("date")
        if title is None and entry_date is None:
            return {"error": "give at least a title or a date to change."}
        return self.notion.update_calendar_entry(
            self.calendar_database_id, entry_id,
            title=str(title).strip() if title is not None else None,
            date_str=str(entry_date).strip() if entry_date is not None else None,
        )

    def _tool_delete_calendar_entry(self, arguments):
        if not self.calendar_database_id:
            return {"error": "No calendar is configured."}
        entry_id = str(arguments.get("entry_id", "")).strip()
        if not entry_id:
            return {"error": "entry_id is required."}
        return self.notion.delete_calendar_entry(entry_id)

    # ---- general Notion ----

    def _tool_notion_search(self, arguments):
        query = str(arguments.get("query", "")).strip()
        return {"results": self.notion.search(query)}

    def _tool_notion_read_page(self, arguments):
        page_id = str(arguments.get("page_id", "")).strip()
        if not page_id:
            return {"error": "page_id is required."}
        title = self.notion.get_page_title(page_id)
        children = self.notion.get_block_children(page_id)
        return {"title": title, "blocks": children}

    def _tool_notion_append_content(self, arguments):
        parent_id = str(arguments.get("parent_id", "")).strip()
        block_type = str(arguments.get("block_type", "")).strip()
        text = str(arguments.get("text", "")).strip()
        if not parent_id or not block_type or not text:
            return {"error": "parent_id, block_type, and text are all required."}
        return self.notion.append_block(parent_id, block_type, text)

    def _tool_notion_update_block_text(self, arguments):
        block_id = str(arguments.get("block_id", "")).strip()
        text = str(arguments.get("text", "")).strip()
        if not block_id or not text:
            return {"error": "block_id and text are both required."}
        return self.notion.update_block_text(block_id, text)

    def _tool_notion_create_page(self, arguments):
        parent_page_id = str(arguments.get("parent_page_id", "")).strip()
        title = str(arguments.get("title", "")).strip()
        if not parent_page_id or not title:
            return {"error": "parent_page_id and title are both required."}
        return self.notion.create_page(parent_page_id, title)

    def _tool_notion_update_page_title(self, arguments):
        page_id = str(arguments.get("page_id", "")).strip()
        title = str(arguments.get("title", "")).strip()
        if not page_id or not title:
            return {"error": "page_id and title are both required."}
        return self.notion.update_page_title(page_id, title)

    def _tool_notion_query_database(self, arguments):
        database_id = str(arguments.get("database_id", "")).strip()
        if not database_id:
            return {"error": "database_id is required."}
        return {"rows": self.notion.query_database(database_id)}

    def _tool_notion_delete(self, arguments):
        target_id = str(arguments.get("id", "")).strip()
        kind = str(arguments.get("kind", "")).strip().lower()
        if not target_id or kind not in ("page", "block"):
            return {"error": "id is required, and kind must be 'page' or 'block'."}
        if kind == "page":
            return self.notion.archive_page(target_id)
        return self.notion.archive_block(target_id)

    _TOOL_HANDLERS = {
        "get_exam_table": _tool_get_exam_table,
        "update_exam_cell": _tool_update_exam_cell,
        "list_todos": _tool_list_todos,
        "add_todo": _tool_add_todo,
        "update_todo": _tool_update_todo,
        "set_todo_checked": _tool_set_todo_checked,
        "delete_todo": _tool_delete_todo,
        "list_calendar_entries": _tool_list_calendar_entries,
        "add_calendar_entry": _tool_add_calendar_entry,
        "update_calendar_entry": _tool_update_calendar_entry,
        "delete_calendar_entry": _tool_delete_calendar_entry,
        "notion_search": _tool_notion_search,
        "notion_read_page": _tool_notion_read_page,
        "notion_append_content": _tool_notion_append_content,
        "notion_update_block_text": _tool_notion_update_block_text,
        "notion_create_page": _tool_notion_create_page,
        "notion_update_page_title": _tool_notion_update_page_title,
        "notion_query_database": _tool_notion_query_database,
        "notion_delete": _tool_notion_delete,
    }
