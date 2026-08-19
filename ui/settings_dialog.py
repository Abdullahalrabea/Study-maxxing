# The Settings dialog: theme picker (applies live), Notion connection
# fields, the LM Studio server URL, and the "week 1 starts on" date that
# drives the to-do list page's auto-syncing week number -- everything that
# used to be either hardcoded or scattered across tabs, now in one place.
#
# Theme changes apply immediately. Notion/LM Studio connection changes are
# saved to QSettings but only take effect on the next app start, since the
# panels that use them (StudyPlannerPanel, the embedded Notion view) are
# built once at startup -- the dialog says so plainly rather than
# pretending otherwise.

import re

from PyQt6.QtCore import QSettings, QDate, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QGroupBox, QDateEdit, QRadioButton, QButtonGroup, QScrollArea, QWidget, QMessageBox,
    QComboBox, QProgressBar
)

import theme
import session_snapshot
import audio_devices
import xtts_voice
from llm_client import LLMClient, LM_STUDIO_BASE_URL as DEFAULT_LM_STUDIO_URL

MIC_TEST_DURATION_MS = 3000


def _extract_youtube_handle(text):
    """Accepts a full channel URL (e.g. youtube.com/@SomeChannel, with or
    without https://, a trailing slash, or extra path) or a bare handle
    (with or without the leading @) -- normalizes down to just the
    handle text, no @. services/youtube_feed.py's resolve_channel_id()
    adds the @ prefix itself if it's missing, so this doesn't need to.
    Returns None for blank input."""
    text = text.strip()
    if not text:
        return None
    match = re.search(r"@([A-Za-z0-9_.-]+)", text)
    return match.group(1) if match else text

SETTINGS_ORG = "StudyWarden"
SETTINGS_APP = "StudyWarden"

SETTINGS_KEY_WEEK_START = "week1_start_date"
DEFAULT_WEEK1_START = QDate(2026, 8, 9)

KEY_NOTION_TOKEN = "connections/notion_token"
KEY_NOTION_TABLE_BLOCK_ID = "connections/notion_table_block_id"
KEY_NOTION_TODO_PAGE_ID = "connections/notion_todo_page_id"
KEY_NOTION_CALENDAR_DATABASE_ID = "connections/notion_calendar_database_id"
KEY_NOTION_EMBED_URL = "connections/notion_embed_url"
KEY_LM_STUDIO_URL = "connections/lm_studio_url"

# Same "attention_video/" namespace youtube_feed.py already uses for its
# own per-handle cache keys (channel_id/, watched/).
KEY_YOUTUBE_CHANNEL_HANDLE = "attention_video/channel_handle"


class WeekSyncWorker(QThread):
    """Pushes the current computed week number to the to-do page's title,
    only writing if it's actually different from what's already there."""
    done = pyqtSignal(int)
    failed = pyqtSignal(str)

    def __init__(self, todo_page, start_date):
        super().__init__()
        self.todo_page = todo_page
        self.start_date = start_date

    def run(self):
        try:
            week = self.todo_page.sync_week_number(self.start_date)
            self.done.emit(week)
        except Exception as e:
            self.failed.emit(str(e))


class ClearDataWorker(QThread):
    """Runs NotionAgent.clear_all_data() off the GUI thread -- it makes one
    real Notion API call per cell/entry/item cleared, which can be slow
    enough to notice for a full table + a busy calendar."""
    done = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, agent):
        super().__init__()
        self.agent = agent

    def run(self):
        try:
            result = self.agent.clear_all_data()
            self.done.emit(result)
        except Exception as e:
            self.failed.emit(str(e))


class ConnectionTestWorker(QThread):
    """Runs LLMClient.check_connection() off the GUI thread for the LM
    Studio "Test Connection" button -- a real HTTP request with its own
    timeout, no reason to block the dialog waiting on it."""
    done = pyqtSignal(bool, str)

    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url

    def run(self):
        client = LLMClient(base_url=self.base_url)
        ok, message = client.check_connection()
        self.done.emit(ok, message)


class SettingsDialog(QDialog):
    def __init__(self, todo_page=None, current_notion=None, current_lm_studio_url=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(520, 640)
        self.todo_page = todo_page
        self._current_notion = current_notion or {}
        self._week_worker = None
        self._clear_data_worker = None
        self._lm_test_worker = None
        self._mic_test_worker = None
        self._mic_test_peak = 0.0
        self._voice_preview_worker = None
        self._settings = QSettings(SETTINGS_ORG, SETTINGS_APP)

        outer = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)

        layout.addWidget(self._build_appearance_group())
        layout.addWidget(self._build_week_group())
        layout.addWidget(self._build_notion_group(self._current_notion))
        layout.addWidget(self._build_data_group())
        layout.addWidget(self._build_lm_studio_group(current_lm_studio_url))
        layout.addWidget(self._build_microphone_group())
        layout.addWidget(self._build_voice_group())
        layout.addWidget(self._build_attention_video_group())
        layout.addWidget(self._build_debt_group())
        layout.addStretch()

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        close_row.addWidget(close_button)
        outer.addLayout(close_row)

    # ---- appearance ----

    def _build_appearance_group(self):
        group = QGroupBox("Appearance")
        layout = QVBoxLayout(group)
        info = QLabel("Pick a theme -- applies immediately, no restart needed.")
        info.setWordWrap(True)
        layout.addWidget(info)

        self._theme_button_group = QButtonGroup(self)
        for theme_id, colors in theme.THEMES.items():
            row = QHBoxLayout()

            swatch = QLabel()
            swatch.setFixedSize(18, 18)
            swatch.setStyleSheet(
                f"background-color: {colors['accent']}; border: 1px solid {colors['border']};"
            )
            row.addWidget(swatch)

            radio = QRadioButton(colors["label"])
            radio.setChecked(theme_id == theme.manager.current_id)
            radio.toggled.connect(lambda checked, tid=theme_id: self._on_theme_picked(checked, tid))
            self._theme_button_group.addButton(radio)
            row.addWidget(radio)
            row.addStretch()
            layout.addLayout(row)

        return group

    def _on_theme_picked(self, checked, theme_id):
        if checked:
            theme.crossfade_theme_change(self.parentWidget(), theme_id)

    # ---- week settings ----

    def _build_week_group(self):
        group = QGroupBox("Week Settings")
        layout = QVBoxLayout(group)

        row = QHBoxLayout()
        row.addWidget(theme.HelpIcon(
            "The to-do list page's title (\"To Do List (Current week:N):\") stays in sync with "
            "this automatically, both right away and every time the app loads. Add to-do items "
            "from the To-Do List tab."
        ))
        row.addWidget(QLabel("Week 1 starts on:"))
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        saved = self._settings.value(SETTINGS_KEY_WEEK_START)
        saved_date = QDate.fromString(saved, "yyyy-MM-dd") if saved else None
        self.date_edit.setDate(saved_date if saved_date and saved_date.isValid() else DEFAULT_WEEK1_START)
        row.addWidget(self.date_edit)

        self.week_save_button = QPushButton("Save")
        self.week_save_button.clicked.connect(self._on_week_save_clicked)
        row.addWidget(self.week_save_button)
        layout.addLayout(row)

        self.week_status_label = QLabel("")
        self.week_status_label.setWordWrap(True)
        layout.addWidget(self.week_status_label)

        if self.todo_page is None:
            self.week_save_button.setEnabled(False)
            self.week_status_label.setText("No to-do list page configured.")
        else:
            self._sync_week(self._current_start_date())

        return group

    def _current_start_date(self):
        qd = self.date_edit.date()
        from datetime import date
        return date(qd.year(), qd.month(), qd.day())

    def _on_week_save_clicked(self):
        self._settings.setValue(SETTINGS_KEY_WEEK_START, self.date_edit.date().toString("yyyy-MM-dd"))
        self._sync_week(self._current_start_date())

    def _sync_week(self, start_date):
        self.week_status_label.setText("Syncing week number with Notion...")
        self.week_save_button.setEnabled(False)
        self._week_worker = WeekSyncWorker(self.todo_page, start_date)
        self._week_worker.done.connect(self._on_week_sync_done)
        self._week_worker.failed.connect(self._on_week_sync_failed)
        self._week_worker.start()

    def _on_week_sync_done(self, week):
        self.week_save_button.setEnabled(True)
        if week <= 0:
            self.week_status_label.setText("Week 1 hasn't started yet according to this date.")
        else:
            self.week_status_label.setText(f"Notion title is up to date -- current week: {week}.")

    def _on_week_sync_failed(self, message):
        self.week_save_button.setEnabled(True)
        self.week_status_label.setText(f"Couldn't sync with Notion: {message}")

    # ---- Notion connection ----

    def _build_notion_group(self, current):
        group = QGroupBox("Notion")
        layout = QFormLayout(group)

        note = QLabel("Saved here, but only take effect the next time Study Maxxing starts.")
        note.setWordWrap(True)
        layout.addRow(note)

        self.notion_token_edit = QLineEdit(current.get("token", ""))
        self.notion_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addRow("Integration token:", self.notion_token_edit)

        self.notion_table_edit = QLineEdit(current.get("table_block_id", ""))
        layout.addRow("Schedule table block ID:", self.notion_table_edit)

        self.notion_todo_edit = QLineEdit(current.get("todo_page_id", ""))
        layout.addRow("To-do list page ID:", self.notion_todo_edit)

        self.notion_calendar_edit = QLineEdit(current.get("calendar_database_id", ""))
        layout.addRow("Study calendar database ID:", self.notion_calendar_edit)

        self.notion_embed_edit = QLineEdit(current.get("embed_url", ""))
        layout.addRow("Embedded page URL:", self.notion_embed_edit)

        button_row = QHBoxLayout()
        save_button = QPushButton("Save Notion settings")
        save_button.clicked.connect(self._on_notion_save_clicked)
        button_row.addWidget(save_button)

        reset_button = QPushButton("Reset Notion settings")
        reset_button.setToolTip(
            "Clears these saved fields (token, table/page/database IDs, embed URL) -- for testing. "
            "Falls back to any environment variable, then the hardcoded defaults in main_window.py."
        )
        reset_button.clicked.connect(self._on_notion_reset_clicked)
        button_row.addWidget(reset_button)
        layout.addRow(button_row)

        self.notion_status_label = QLabel("")
        self.notion_status_label.setWordWrap(True)
        layout.addRow(self.notion_status_label)

        return group

    def _on_notion_reset_clicked(self):
        confirmed = QMessageBox.question(
            self, "Reset Notion settings",
            "Clear all saved Notion settings (token, table/page/database IDs, embed URL)?\n\n"
            "This only clears what's saved here -- it doesn't touch anything in your actual Notion "
            "workspace. After this, the app falls back to any environment variable, then the "
            "hardcoded defaults, the next time it starts.",
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        for key in (
            KEY_NOTION_TOKEN, KEY_NOTION_TABLE_BLOCK_ID, KEY_NOTION_TODO_PAGE_ID,
            KEY_NOTION_CALENDAR_DATABASE_ID, KEY_NOTION_EMBED_URL,
        ):
            self._settings.remove(key)
        self.notion_token_edit.clear()
        self.notion_table_edit.clear()
        self.notion_todo_edit.clear()
        self.notion_calendar_edit.clear()
        self.notion_embed_edit.clear()
        self.notion_status_label.setText("Cleared -- restart Study Maxxing for this to take effect.")

    def _on_notion_save_clicked(self):
        self._settings.setValue(KEY_NOTION_TOKEN, self.notion_token_edit.text().strip())
        self._settings.setValue(KEY_NOTION_TABLE_BLOCK_ID, self.notion_table_edit.text().strip())
        self._settings.setValue(KEY_NOTION_TODO_PAGE_ID, self.notion_todo_edit.text().strip())
        self._settings.setValue(KEY_NOTION_CALENDAR_DATABASE_ID, self.notion_calendar_edit.text().strip())
        self._settings.setValue(KEY_NOTION_EMBED_URL, self.notion_embed_edit.text().strip())
        self.notion_status_label.setText("Saved -- restart Study Maxxing for this to take effect.")

    # ---- clear Notion data (table/calendar/to-do -- NOT the same as "Reset
    # Notion settings" above, which only clears saved connection fields) ----

    def _build_data_group(self):
        group = QGroupBox("Data")
        layout = QVBoxLayout(group)

        row = QHBoxLayout()
        row.addWidget(theme.HelpIcon(
            "Wipes every cell in the schedule table (course names and every field, for every "
            "course column), archives every entry in the study calendar, and archives every "
            "item in the to-do list -- restores all three to an empty starting state. This is "
            "real content in your actual Notion workspace, not a saved setting -- but Notion's "
            "own delete is archive-to-Trash, so anything cleared is recoverable from Notion's "
            "UI afterward if you clear something by mistake."
        ))
        row.addWidget(QLabel("Clear the schedule table, calendar, and to-do list"))
        row.addStretch()
        layout.addLayout(row)

        self.clear_data_button = QPushButton("Clear all data")
        self.clear_data_button.clicked.connect(self._on_clear_data_clicked)
        layout.addWidget(self.clear_data_button)

        self.clear_data_status_label = QLabel("")
        self.clear_data_status_label.setWordWrap(True)
        layout.addWidget(self.clear_data_status_label)

        return group

    def _on_clear_data_clicked(self):
        confirmed = QMessageBox.question(
            self, "Clear all data",
            "This clears the SCHEDULE TABLE (every course and field), the STUDY CALENDAR (every "
            "entry), and the TO-DO LIST (every item) in your real Notion workspace.\n\n"
            "This does not touch your saved connection settings above, and Notion's own delete "
            "is archive-to-Trash, so this is recoverable from Notion's UI afterward -- but it "
            "clears real content right now. Continue?",
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return

        token = self._current_notion.get("token")
        table_block_id = self._current_notion.get("table_block_id")
        if not token or not table_block_id:
            self.clear_data_status_label.setText("Notion isn't configured -- nothing to clear.")
            return
        calendar_database_id = self._current_notion.get("calendar_database_id")

        from notion_agent import NotionAgent
        agent = NotionAgent(
            token, table_block_id, calendar_database_id=calendar_database_id, todo_page=self.todo_page,
        )
        self.clear_data_button.setEnabled(False)
        self.clear_data_status_label.setText("Clearing...")
        self._clear_data_worker = ClearDataWorker(agent)
        self._clear_data_worker.done.connect(self._on_clear_data_done)
        self._clear_data_worker.failed.connect(self._on_clear_data_failed)
        self._clear_data_worker.start()

    def _on_clear_data_done(self, result):
        self.clear_data_button.setEnabled(True)
        self.clear_data_status_label.setText(
            f"Cleared {result['table_cells_cleared']} table cell(s), "
            f"{result['calendar_entries_cleared']} calendar entry/entries, and "
            f"{result['todo_items_cleared']} to-do item(s)."
        )

    def _on_clear_data_failed(self, message):
        self.clear_data_button.setEnabled(True)
        self.clear_data_status_label.setText(f"Couldn't clear everything: {message}")

    # ---- LM Studio connection ----

    def _build_lm_studio_group(self, current_url):
        group = QGroupBox("LM Studio")
        layout = QFormLayout(group)

        note = QLabel(
            "The local server URL LM Studio exposes once you've loaded a model and clicked "
            "\"Start Server\" in the Developer tab. Saved here, but only takes effect the next "
            "time Study Maxxing starts."
        )
        note.setWordWrap(True)
        layout.addRow(note)

        self.lm_studio_url_edit = QLineEdit(current_url or DEFAULT_LM_STUDIO_URL)
        layout.addRow("Server URL:", self.lm_studio_url_edit)

        button_row = QHBoxLayout()
        save_button = QPushButton("Save LM Studio settings")
        save_button.clicked.connect(self._on_lm_studio_save_clicked)
        button_row.addWidget(save_button)

        # Checks the URL currently typed in the field above (not
        # necessarily the saved one) -- lets you verify a URL BEFORE
        # saving it, and catches "server down"/"no model loaded" right
        # here rather than discovering it mid-lockdown-session later.
        self.lm_studio_test_button = QPushButton("Test Connection")
        self.lm_studio_test_button.clicked.connect(self._on_lm_studio_test_clicked)
        button_row.addWidget(self.lm_studio_test_button)
        layout.addRow(button_row)

        self.lm_studio_status_label = QLabel("")
        self.lm_studio_status_label.setWordWrap(True)
        layout.addRow(self.lm_studio_status_label)

        return group

    def _on_lm_studio_save_clicked(self):
        self._settings.setValue(KEY_LM_STUDIO_URL, self.lm_studio_url_edit.text().strip())
        self.lm_studio_status_label.setText("Saved -- restart Study Maxxing for this to take effect.")

    def _on_lm_studio_test_clicked(self):
        if self._lm_test_worker is not None and self._lm_test_worker.isRunning():
            return
        self.lm_studio_test_button.setEnabled(False)
        self.lm_studio_status_label.setText("Testing connection...")
        url = self.lm_studio_url_edit.text().strip() or DEFAULT_LM_STUDIO_URL
        self._lm_test_worker = ConnectionTestWorker(url)
        self._lm_test_worker.done.connect(self._on_lm_studio_test_done)
        self._lm_test_worker.start()

    def _on_lm_studio_test_done(self, ok, message):
        self.lm_studio_test_button.setEnabled(True)
        # Plain text, no glyph prefix (checkmark/X symbols) -- confirmed
        # earlier the same day that non-emoji Unicode symbols can render
        # as empty boxes on this machine's font setup; check_connection()'s
        # own message is already self-describing either way.
        self.lm_studio_status_label.setText(message)

    # ---- microphone (see services/audio_devices.py -- shared by the
    # Monitor Lockdown voice assistant's push-to-talk, ui/voice_assistant_panel.py) ----

    def _build_microphone_group(self):
        group = QGroupBox("Microphone")
        layout = QVBoxLayout(group)

        note = QLabel(
            "Which microphone the voice assistant listens to during Monitor Lockdown "
            "(hold T, or hold the mic button, to ask a question). Takes effect immediately -- "
            "no restart needed, since push-to-talk re-checks this every time you hold T."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self._mic_devices = audio_devices.list_input_devices()
        saved_name = audio_devices.get_saved_device_name()

        combo_row = QHBoxLayout()
        self.mic_combo = QComboBox()
        if self._mic_devices:
            selected_index = 0
            for i, (device_index, name) in enumerate(self._mic_devices):
                self.mic_combo.addItem(name, device_index)
                if name == saved_name:
                    selected_index = i
            self.mic_combo.setCurrentIndex(selected_index)
        else:
            self.mic_combo.addItem("No input devices found")
            self.mic_combo.setEnabled(False)
        self.mic_combo.currentIndexChanged.connect(self._on_mic_device_changed)
        combo_row.addWidget(self.mic_combo, 1)
        layout.addLayout(combo_row)

        test_row = QHBoxLayout()
        self.mic_test_button = QPushButton("Test Mic (3s)")
        self.mic_test_button.setEnabled(bool(self._mic_devices))
        self.mic_test_button.clicked.connect(self._on_mic_test_clicked)
        test_row.addWidget(self.mic_test_button)
        self.mic_level_bar = QProgressBar()
        self.mic_level_bar.setRange(0, 100)
        self.mic_level_bar.setTextVisible(False)
        test_row.addWidget(self.mic_level_bar, 1)
        layout.addLayout(test_row)

        self.mic_status_label = QLabel(
            f"Currently using: {saved_name}" if saved_name else "Using the system default input device."
        )
        self.mic_status_label.setWordWrap(True)
        layout.addWidget(self.mic_status_label)

        return group

    def _on_mic_device_changed(self, index):
        if not self._mic_devices or index < 0:
            return
        name = self.mic_combo.currentText()
        audio_devices.save_device_name(name)
        self.mic_status_label.setText(f"Now using: {name}")

    def _on_mic_test_clicked(self):
        if not self._mic_devices:
            return
        device_index = self.mic_combo.currentData()
        self.mic_test_button.setEnabled(False)
        self.mic_level_bar.setValue(0)
        self._mic_test_peak = 0.0
        self.mic_status_label.setText("Recording for 3 seconds -- say something!")
        self._mic_test_worker = audio_devices.MicRecorderThread(device_index=device_index, parent=self)
        self._mic_test_worker.level_updated.connect(self._on_mic_test_level)
        self._mic_test_worker.finished_recording.connect(self._on_mic_test_done)
        self._mic_test_worker.start()
        QTimer.singleShot(MIC_TEST_DURATION_MS, self._mic_test_worker.stop)

    def _on_mic_test_level(self, level):
        self._mic_test_peak = max(self._mic_test_peak, level)
        self.mic_level_bar.setValue(round(level * 100))

    def _on_mic_test_done(self, audio):
        self._mic_test_worker = None
        self.mic_test_button.setEnabled(True)
        if audio is None:
            self.mic_status_label.setText(
                "Couldn't record from that device -- check it's plugged in and not in use elsewhere."
            )
            return
        peak_pct = round(self._mic_test_peak * 100)
        self.mic_level_bar.setValue(peak_pct)
        if peak_pct < 3:
            self.mic_status_label.setText(
                f"Peak level: {peak_pct}% -- very quiet. Check this is the right device and it isn't muted."
            )
        else:
            self.mic_status_label.setText(f"Peak level: {peak_pct}% -- picked up sound, looks good!")

    # ---- voice (see services/xtts_voice.py -- cloned TTS voice for the
    # Monitor Lockdown voice assistant, ui/voice_assistant_panel.py) ----

    def _build_voice_group(self):
        group = QGroupBox("Voice")
        layout = QVBoxLayout(group)

        note = QLabel(
            "Which voice the voice assistant speaks answers in during Monitor Lockdown. "
            "\"Default\" is the instant Windows voice; cloned voices (from AI/Voices/) use "
            "XTTS-v2 -- much more expressive, but real neural synthesis: expect a genuine "
            "multi-second delay before it starts talking, not instant."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self._voices = xtts_voice.list_voices()
        saved_name = xtts_voice.get_saved_voice_name()

        self.voice_combo = QComboBox()
        self.voice_combo.addItem("Default (Windows)", None)
        selected_index = 0
        for i, (name, _path) in enumerate(self._voices, start=1):
            self.voice_combo.addItem(name, name)
            if name == saved_name:
                selected_index = i
        self.voice_combo.setCurrentIndex(selected_index)
        self.voice_combo.currentIndexChanged.connect(self._on_voice_changed)
        layout.addWidget(self.voice_combo)

        self.voice_preview_button = QPushButton("Preview Voice")
        self.voice_preview_button.clicked.connect(self._on_voice_preview_clicked)
        layout.addWidget(self.voice_preview_button)

        self.voice_status_label = QLabel(
            f"Currently using: {saved_name}" if saved_name else "Using the default Windows voice."
        )
        self.voice_status_label.setWordWrap(True)
        layout.addWidget(self.voice_status_label)

        if not self._voices:
            empty_note = QLabel("No voice clips found in AI/Voices/ -- only the default voice is available.")
            empty_note.setWordWrap(True)
            layout.addWidget(empty_note)

        return group

    def _on_voice_changed(self, index):
        name = self.voice_combo.currentData()  # None for Default, else the voice name string
        xtts_voice.save_voice_name(name)
        self.voice_status_label.setText(f"Now using: {name}" if name else "Using the default Windows voice.")

    def _on_voice_preview_clicked(self):
        name = self.voice_combo.currentData()
        if name is None:
            self.voice_status_label.setText("The default voice doesn't need a preview -- it's instant.")
            return
        path = xtts_voice.resolve_voice_path(name)
        if path is None:
            self.voice_status_label.setText("Couldn't find that voice's reference clip.")
            return
        self.voice_preview_button.setEnabled(False)
        self.voice_status_label.setText(
            "Generating preview... this can take a while the first time (downloads the model)."
        )
        self._voice_preview_worker = xtts_voice.XTTSSpeakThread(
            "Hello! This is a preview of how this voice sounds.", path, self
        )
        self._voice_preview_worker.finished_speaking.connect(self._on_voice_preview_done)
        self._voice_preview_worker.start()

    def _on_voice_preview_done(self):
        self.voice_preview_button.setEnabled(True)
        self.voice_status_label.setText("Preview finished.")
        self._voice_preview_worker = None

    # ---- attention video (see ui/attention_video.py -- the small corner
    # YouTube player during Monitor Lockdown) ----

    def _build_attention_video_group(self):
        group = QGroupBox("Attention Video")
        layout = QVBoxLayout(group)

        note = QLabel(
            "The small YouTube player shown during Monitor Lockdown always plays from ONE "
            "channel (a deliberate, user-confirmed exception to the app's anti-distraction "
            "design). Paste that channel's URL, or just its @handle, to change it."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.youtube_channel_edit = QLineEdit()
        self.youtube_channel_edit.setPlaceholderText("e.g. https://www.youtube.com/@SomeChannel or @SomeChannel")
        saved_handle = self._settings.value(KEY_YOUTUBE_CHANNEL_HANDLE)
        if saved_handle:
            self.youtube_channel_edit.setText(saved_handle)
        layout.addWidget(self.youtube_channel_edit)

        save_button = QPushButton("Save Channel")
        save_button.clicked.connect(self._on_save_youtube_channel_clicked)
        layout.addWidget(save_button)

        self.youtube_channel_status_label = QLabel(
            f"Currently: @{saved_handle} -- takes effect next time Monitor Lockdown starts."
            if saved_handle else "Using the default channel."
        )
        self.youtube_channel_status_label.setWordWrap(True)
        layout.addWidget(self.youtube_channel_status_label)

        return group

    def _on_save_youtube_channel_clicked(self):
        handle = _extract_youtube_handle(self.youtube_channel_edit.text())
        if handle is None:
            self._settings.remove(KEY_YOUTUBE_CHANNEL_HANDLE)
            self.youtube_channel_status_label.setText("Using the default channel.")
            return
        self._settings.setValue(KEY_YOUTUBE_CHANNEL_HANDLE, handle)
        self.youtube_channel_status_label.setText(
            f"Currently: @{handle} -- takes effect next time Monitor Lockdown starts."
        )

    # ---- debt (see core/session_snapshot.py -- unfinished study time from
    # early-leave sessions, and push-ups owed from an interrupted penalty) ----

    def _build_debt_group(self):
        group = QGroupBox("Debt")
        outer_layout = QVBoxLayout(group)

        row = QHBoxLayout()
        row.addWidget(theme.HelpIcon(
            "Unfinished study time from a calendar-linked session you left early (gets folded into "
            "that course/field's next session automatically), and any push-ups still owed from an "
            "interrupted penalty (see the Focus Session page's nudge). Clear an entry here to forgive "
            "it without actually doing/finishing it."
        ))
        row.addWidget(QLabel("Outstanding study debt and owed push-ups"))
        row.addStretch()
        outer_layout.addLayout(row)

        self.debt_list_layout = QVBoxLayout()
        outer_layout.addLayout(self.debt_list_layout)

        clear_all_row = QHBoxLayout()
        clear_all_row.addStretch()
        self.clear_all_debt_button = QPushButton("Clear all debt")
        self.clear_all_debt_button.clicked.connect(self._on_clear_all_debt_clicked)
        clear_all_row.addWidget(self.clear_all_debt_button)
        outer_layout.addLayout(clear_all_row)

        self._refresh_debt_list()
        return group

    def _refresh_debt_list(self):
        """Clears and rebuilds the debt list from whatever's currently on
        disk -- called once at construction and again after any clear."""
        while self.debt_list_layout.count():
            item = self.debt_list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        debts = session_snapshot.load_all_debts()
        penalty = session_snapshot.load_penalty()
        has_any = bool(debts) or penalty is not None

        for entry in debts.values():
            row = QHBoxLayout()
            label = QLabel(
                f"{entry['course']} {entry['field']} -- {round(entry['shortfall_seconds'] / 60)} min owed "
                f"({entry['completion_pct']}% done, left {entry['saved_at']})"
            )
            label.setWordWrap(True)
            row.addWidget(label, 1)
            clear_button = QPushButton("Clear")
            clear_button.clicked.connect(
                lambda _checked, c=entry["course"], f=entry["field"]: self._on_clear_one_debt_clicked(c, f)
            )
            row.addWidget(clear_button)
            self.debt_list_layout.addLayout(row)

        if penalty is not None:
            row = QHBoxLayout()
            label = QLabel(f"Push-ups owed: {penalty['reps_owed']} (since {penalty['saved_at']})")
            label.setWordWrap(True)
            row.addWidget(label, 1)
            clear_button = QPushButton("Clear")
            clear_button.clicked.connect(self._on_clear_penalty_clicked)
            row.addWidget(clear_button)
            self.debt_list_layout.addLayout(row)

        if not has_any:
            self.debt_list_layout.addWidget(QLabel("No outstanding debt."))

        self.clear_all_debt_button.setEnabled(has_any)

    def _on_clear_one_debt_clicked(self, course, field):
        session_snapshot.clear_debt(course, field)
        self._refresh_debt_list()

    def _on_clear_penalty_clicked(self):
        session_snapshot.clear_penalty()
        self._refresh_debt_list()

    def _on_clear_all_debt_clicked(self):
        confirmed = QMessageBox.question(
            self, "Clear all debt",
            "Forgive every outstanding study-time debt and any owed push-ups? This just clears the "
            "record -- it doesn't add the time back or complete the reps for you.",
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        for entry in session_snapshot.load_all_debts().values():
            session_snapshot.clear_debt(entry["course"], entry["field"])
        session_snapshot.clear_penalty()
        self._refresh_debt_list()
