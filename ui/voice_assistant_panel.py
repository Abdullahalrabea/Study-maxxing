# Push-to-talk voice Q&A during Monitor Lockdown: HOLD the mic button (or
# hold the "T" key) to record a spoken question, RELEASE to stop and get a
# spoken answer from the LLM using whichever slide is CURRENTLY SCROLLED
# INTO VIEW as context (see LockdownWindow._current_slide_text()) -- not a
# full embedding search across the whole deck. The page you're actually
# looking at right now is a simpler and more reliable signal than a
# keyword/embedding guess would be, for the same reason push-to-talk
# itself won out over fully-automatic ambient question detection: prefer
# the more literal, more reliable answer over a guess.
#
# Genuinely PRESS-AND-HOLD, not a toggle: released as soon as you let go
# of T/the button, not on a second press. QPushButton.setShortcut() can't
# express that (a Qt shortcut is a single "activated" event, no separate
# press/release). T itself is detected via lockdown_os.HoldKeyDetector --
# GetAsyncKeyState polling, NOT a Qt KeyPress/KeyRelease event filter.
# An earlier version used a QApplication-wide event filter for this, but
# in practice it sometimes desynced from the real physical key state (T
# would visually register LISTENING, then almost immediately "release"
# on its own even while still held down) -- the same class of Qt-focus-
# dependent unreliability ExitHotkeyDetector's own docstring already
# explains why Ctrl+Shift+Alt uses this same polling technique instead
# of a Qt-level shortcut/hook.
#
# State machine (all four images user-supplied, ui/Images/):
#   IDLE      (catnuetral.jpg)       -- not listening, button not held
#   LISTENING (catlisten.jpg)        -- recording right now, T/button held
#   THINKING  (hmm-cat-thinking.jpg) -- transcribing / waiting on the LLM
#   TALKING   (cattalking.gif)       -- speaking the answer aloud
#
# Pressing T/the button while TALKING interrupts the in-progress speech
# (pyttsx3's documented cross-thread stop()) and immediately starts a new
# recording, rather than being ignored/disabled during playback. Pressing
# it while THINKING is "nvm" -- see _cancel_thinking() -- gives up on the
# in-flight transcription/LLM call and goes straight back to idle rather
# than forcing you to wait it out.
#
# A live input-level meter (see audio_devices.MicRecorderThread's
# level_updated) shows under the status label while LISTENING -- direct,
# in-the-moment proof the selected mic (see the Settings "Microphone"
# section) is actually picking up sound, not just a guess after
# transcription silently comes back empty.
#
# TALKING can speak in a CLONED voice instead of the default SAPI5 one
# (see Settings' "Voice" section, services/xtts_voice.py) -- XTTS-v2,
# cloning from a reference clip you provide. Real neural synthesis, not
# instant like SAPI5: expect a real multi-second delay before playback
# starts.

from pathlib import Path

from PyQt6.QtCore import Qt, QSize, QThread, pyqtSignal
from PyQt6.QtGui import QPixmap, QMovie
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton, QProgressBar

import audio_devices
import lockdown_os
import xtts_voice
import theme

IMAGES_DIR = Path(__file__).resolve().parent / "Images"
IDLE_IMAGE = IMAGES_DIR / "catnuetral.jpg"
LISTEN_IMAGE = IMAGES_DIR / "catlisten.jpg"
THINK_IMAGE = IMAGES_DIR / "hmm-cat-thinking.jpg"
TALK_IMAGE = IMAGES_DIR / "cattalking.gif"

CAT_SIZE = 96
MIN_RECORDING_SECONDS = 0.3  # shorter than this is almost certainly a stray press, not a real question


class _TranscribeAndAnswerThread(QThread):
    """Off-GUI-thread: transcribes the recorded question (faster-whisper,
    loaded once and reused for every question this run), then asks the
    LLM using the given slide text as context (see
    llm_client.answer_slide_question()). Both steps live here so the GUI
    thread never blocks on either the local Whisper model or the local
    LLM call."""
    answer_ready = pyqtSignal(str, str)  # (question_text, answer_text)
    failed = pyqtSignal(str)

    _whisper_model = None  # class-level cache -- shared across every question, loaded on first use

    def __init__(self, llm, audio, slide_text, parent=None):
        super().__init__(parent)
        self.llm = llm
        self.audio = audio
        self.slide_text = slide_text

    @classmethod
    def _get_whisper(cls):
        if cls._whisper_model is None:
            from faster_whisper import WhisperModel
            cls._whisper_model = WhisperModel("base.en", device="cpu", compute_type="int8")
        return cls._whisper_model

    def run(self):
        try:
            model = self._get_whisper()
            segments, _ = model.transcribe(self.audio, language="en")
            question = "".join(segment.text for segment in segments).strip()
        except Exception as e:
            self.failed.emit(f"Couldn't transcribe that: {e}")
            return

        if not question:
            self.failed.emit("Didn't catch a question -- try again.")
            return

        try:
            answer = self.llm.answer_slide_question(self.slide_text, question)
        except Exception as e:
            self.failed.emit(f"Couldn't get an answer: {e}")
            return

        self.answer_ready.emit(question, answer)


class _TTSThread(QThread):
    """Speaks text aloud via pyttsx3 -- but NOT by calling pyttsx3
    directly on this QThread. pyttsx3's Windows driver (SAPI5, COM-based)
    relies on a Windows message pump to detect when speech actually
    finishes; even with COM explicitly initialized on this thread
    (pythoncom.CoInitialize()), running it inside a QThread was confirmed
    -- via a real timed test, not just a guess -- to silently do nothing:
    runAndWait() returned in ~0.1s instead of the ~8s a real utterance of
    similar length actually takes on this machine, with no exception at
    all. Running the actual pyttsx3 call in a genuinely separate OS
    PROCESS instead (confirmed via the same kind of timed test: ~8.5s,
    matching a real main-thread call) sidesteps whatever about the
    surrounding Qt app's own COM/threading state was interfering, since a
    freshly spawned process starts with a completely clean apartment.
    Text is passed via a temp file rather than argv, to avoid command-
    line length/escaping issues for longer answers.

    Interrupting speech (see VoiceAssistantPanel._interrupt()) is
    process.terminate() rather than pyttsx3's own engine.stop() -- more
    reliable, and it's the only lever available anyway once synthesis is
    happening in a different process.

    finished_speaking is emitted from a `finally`, not just the happy
    path -- a failure here must never leave the panel stuck showing
    "Answering..." forever with no way back to idle/listening."""
    finished_speaking = pyqtSignal()

    _SPEAK_SCRIPT = (
        "import sys, pyttsx3\n"
        "with open(sys.argv[1], 'r', encoding='utf-8') as f:\n"
        "    text = f.read()\n"
        "engine = pyttsx3.init()\n"
        "engine.say(text)\n"
        "engine.runAndWait()\n"
    )

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.text = text
        self._process = None

    def run(self):
        import subprocess
        import sys
        import tempfile
        import os

        fd, path = tempfile.mkstemp(suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.text)
            self._process = subprocess.Popen(
                [sys.executable, "-c", self._SPEAK_SCRIPT, path],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self._process.wait()
        except Exception as e:
            print(f"[VoiceAssistant] TTS failed: {e}")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
            self.finished_speaking.emit()

    def stop(self):
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()


class VoiceAssistantPanel(QWidget):
    """Meant to sit in LockdownWindow's top row, beside the timer widget.

    get_current_slide_text: zero-arg callable returning the text of
    whichever slide is currently scrolled into view (see LockdownWindow.
    _current_slide_text()) -- called fresh every time a question is
    asked, never cached, so it always reflects wherever you've actually
    scrolled to by the time you finish asking."""

    IDLE, LISTENING, THINKING, TALKING = range(4)

    def __init__(self, llm, get_current_slide_text, parent=None):
        super().__init__(parent)
        self.llm = llm
        self._get_current_slide_text = get_current_slide_text
        self._state = self.IDLE
        self._recorder = None
        self._worker = None
        self._tts = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = theme.Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(2)
        outer.addWidget(card)

        self._idle_pixmap = QPixmap(str(IDLE_IMAGE)).scaled(
            CAT_SIZE, CAT_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        self._listen_pixmap = QPixmap(str(LISTEN_IMAGE)).scaled(
            CAT_SIZE, CAT_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        self._think_pixmap = QPixmap(str(THINK_IMAGE)).scaled(
            CAT_SIZE, CAT_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        self._talk_movie = QMovie(str(TALK_IMAGE))
        self._talk_movie.setScaledSize(QSize(CAT_SIZE, CAT_SIZE))

        self.image_label = QLabel()
        self.image_label.setFixedSize(CAT_SIZE, CAT_SIZE)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.image_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setFixedWidth(CAT_SIZE + 20)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # Live mic input level -- only meaningful/visible while LISTENING
        # (see _on_level_updated()). Direct, real-time proof the selected
        # device is actually picking up sound, rather than only finding
        # out after a silent recording comes back as "Didn't catch that".
        self.level_bar = QProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setTextVisible(False)
        self.level_bar.setFixedHeight(6)
        self.level_bar.setVisible(False)
        layout.addWidget(self.level_bar)

        self._apply_theme(theme.manager.colors)
        theme.manager.changed.connect(self._on_theme_changed)

        self.mic_button = QPushButton("🎤 Ask (hold T)")
        self.mic_button.setCheckable(True)
        self.mic_button.pressed.connect(self._on_hold_started)
        self.mic_button.released.connect(self._on_hold_released)
        layout.addWidget(self.mic_button)

        # GetAsyncKeyState polling (see lockdown_os.HoldKeyDetector), not
        # a Qt key event filter -- see the module docstring for why.
        # Started immediately (not gated behind LockdownWindow's own
        # start_lockdown()/stop_lockdown() lifecycle) since polling is
        # harmless with nothing physically holding T; stopped in
        # shutdown().
        self._t_key = lockdown_os.HoldKeyDetector(lockdown_os.VK_T, parent=self)
        self._t_key.started.connect(self._on_hold_started)
        self._t_key.stopped.connect(self._on_hold_released)
        self._t_key.start()

        self._set_state(self.IDLE)

    def _apply_theme(self, colors):
        self.status_label.setStyleSheet(f"font-size: 10px; color: {colors['text_dim']};")
        self.level_bar.setStyleSheet(f"""
            QProgressBar {{
                background-color: {colors['bg']};
                border: none;
                border-radius: 3px;
            }}
            QProgressBar::chunk {{
                background-color: {colors['accent']};
                border-radius: 3px;
            }}
        """)

    def _on_theme_changed(self, theme_id, colors):
        self._apply_theme(colors)

    @property
    def is_idle(self):
        """True unless a voice interaction (recording, transcribing/
        waiting on the LLM, or speaking) is actively in progress -- lets
        LockdownWindow's periodic engagement judgment (see
        _run_engagement_judgment()) defer itself rather than competing
        with an active voice question for the LLM's attention."""
        return self._state == self.IDLE

    # ---- state machine ----

    # Button text below deliberately avoids the U+23xx "media control
    # symbols" block (⏹ ⏸ ⏮ ⏭ etc.) -- confirmed via a real screenshot
    # from an actual run that at least some of these render as empty
    # boxes on this machine's font setup, unlike the 🎤 emoji (a much
    # more universally-covered block) or plain ASCII, both of which
    # render fine. See spotify_widget.py for the same fix applied there.

    def _set_state(self, state):
        self._state = state
        self.mic_button.setChecked(state in (self.LISTENING, self.TALKING))
        self.level_bar.setVisible(state == self.LISTENING)
        if state != self.LISTENING:
            self.level_bar.setValue(0)
        if state == self.IDLE:
            self._talk_movie.stop()
            self.image_label.setMovie(None)
            self.image_label.setPixmap(self._idle_pixmap)
            self.mic_button.setText("🎤 Ask (hold T)")
            self.mic_button.setEnabled(True)
            self.status_label.setText("")
        elif state == self.LISTENING:
            self._talk_movie.stop()
            self.image_label.setMovie(None)
            self.image_label.setPixmap(self._listen_pixmap)
            self.mic_button.setText("Listening...")
            self.mic_button.setEnabled(True)
            self.status_label.setText("Listening…")
        elif state == self.THINKING:
            self._talk_movie.stop()
            self.image_label.setMovie(None)
            self.image_label.setPixmap(self._think_pixmap)
            self.mic_button.setText("X Nvm (T)")
            self.mic_button.setEnabled(True)  # stays holdable specifically so it can be cancelled -- see _cancel_thinking()
            self.status_label.setText("Thinking…")
        elif state == self.TALKING:
            self.image_label.setMovie(self._talk_movie)
            self._talk_movie.start()
            self.mic_button.setText("🎤 Hold to interrupt (T)")
            self.mic_button.setEnabled(True)  # stays holdable specifically so it can be interrupted
            self.status_label.setText("Answering…")

    def _on_hold_started(self):
        """Fires the instant T/the button is actually pressed down --
        NOT on release, and not a toggle (see the module docstring for
        why a plain QPushButton shortcut can't express this). Same T/
        button press does the contextually right immediate thing in
        every state: start listening, interrupt-and-relisten, or (see
        _cancel_thinking()) give up and go back to idle."""
        if self._state == self.IDLE:
            self._start_listening()
        elif self._state == self.TALKING:
            self._interrupt()
        elif self._state == self.THINKING:
            self._cancel_thinking()
        # LISTENING (e.g. T pressed while already holding the mouse
        # button, or vice versa): no-op, nothing new to start

    def _on_hold_released(self):
        if self._state == self.LISTENING:
            self._stop_listening()
        # anything else: releasing T/the button when it wasn't the thing
        # that started the current recording is a no-op, not an error

    def _start_listening(self):
        self._set_state(self.LISTENING)
        device_index = audio_devices.resolve_device_index(audio_devices.get_saved_device_name())
        self._recorder = audio_devices.MicRecorderThread(device_index=device_index, parent=self)
        self._recorder.level_updated.connect(self._on_level_updated)
        self._recorder.finished_recording.connect(self._on_recording_done)
        self._recorder.start()

    def _stop_listening(self):
        if self._recorder is not None:
            self._recorder.stop()
        self._set_state(self.THINKING)

    def _interrupt(self):
        if self._tts is not None:
            self._tts.stop()
        self._start_listening()

    def _on_level_updated(self, level):
        if self._state == self.LISTENING:
            self.level_bar.setValue(round(level * 100))

    def _on_recording_done(self, audio):
        self._recorder = None
        if audio is None or len(audio) < audio_devices.SAMPLE_RATE * MIN_RECORDING_SECONDS:
            self._set_state(self.IDLE)
            self.status_label.setText("Didn't catch that")
            return

        slide_text = ""
        try:
            slide_text = self._get_current_slide_text() or ""
        except Exception:
            pass  # missing slide context just means a more general answer, not a failure

        self._worker = _TranscribeAndAnswerThread(self.llm, audio, slide_text, self)
        self._worker.answer_ready.connect(self._on_answer_ready)
        self._worker.failed.connect(self._on_answer_failed)
        self._worker.start()

    def _on_answer_ready(self, question, answer):
        if self.sender() is not self._worker:
            return  # cancelled ("nvm") before this arrived -- see _cancel_thinking()
        self._worker = None
        self._speak(answer)

    def _on_answer_failed(self, message):
        if self.sender() is not self._worker:
            return
        self._worker = None
        self._set_state(self.IDLE)
        self.status_label.setText(message)

    def _cancel_thinking(self):
        """"nvm" -- gives up on the in-flight transcription/LLM call and
        goes straight back to IDLE. Neither faster-whisper's transcribe()
        nor the blocking LLM HTTP request has a real cancellation hook,
        so the worker thread itself just keeps running quietly in the
        background until it finishes on its own -- orphaning it here
        (self._worker no longer points at it) means its eventual answer_
        ready/failed gets caught by the `self.sender() is not self.
        _worker` staleness check above and silently discarded, the same
        pattern _on_speaking_done() already uses for a superseded TTS
        thread."""
        self._worker = None
        self._set_state(self.IDLE)

    def _speak(self, text):
        self._set_state(self.TALKING)
        # Cloned voice (see Settings' "Voice" section) if one's picked
        # and its reference clip is still present, otherwise the plain
        # default SAPI5 voice -- resolved fresh every time, so a
        # mid-session Settings change takes effect on the very next
        # answer, no restart needed (same pattern as the mic device
        # picker's resolve-fresh-every-hold approach).
        voice_path = xtts_voice.resolve_voice_path(xtts_voice.get_saved_voice_name())
        if voice_path is not None:
            # First XTTS use in this run of the app loads the model from
            # disk -- confirmed for real to take ~40s (vs ~3-5s per
            # sentence once loaded), a genuinely long wait "Answering…"
            # alone doesn't explain. Only shown pre-load; _set_state()'s
            # normal "Answering…" already covers every call after that.
            if xtts_voice.XTTSEngine._tts is None:
                self.status_label.setText("Loading voice model (~1 min the first time)…")
            self._tts = xtts_voice.XTTSSpeakThread(text, voice_path, self)
        else:
            self._tts = _TTSThread(text, self)
        self._tts.finished_speaking.connect(self._on_speaking_done)
        self._tts.start()

    def _on_speaking_done(self):
        # Only the TTS thread that's still actually current gets to move
        # the state on -- an interrupted/superseded thread's late
        # finished_speaking (its runAndWait() only returns once stop()
        # actually takes effect) must not undo whatever _interrupt()
        # already moved on to.
        if self.sender() is not self._tts:
            return
        if self._state == self.TALKING:
            self._set_state(self.IDLE)
        self._tts = None

    def shutdown(self):
        """Called from LockdownWindow.stop_lockdown() -- stops the T-key
        poller and any in-flight recording/speech so nothing keeps
        running/listening once lockdown ends."""
        self._t_key.stop()
        if self._recorder is not None:
            self._recorder.stop()
            self._recorder.wait(500)
        if self._tts is not None:
            self._tts.stop()
            self._tts.wait(500)
