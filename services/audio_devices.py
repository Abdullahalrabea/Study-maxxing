# Shared mic-device handling for the voice assistant's push-to-talk
# capture (see ui/voice_assistant_panel.py) and its Settings picker/test
# (see ui/settings_dialog.py) -- one place for listing real input
# devices, remembering which one the user picked, and actually
# recording from it, so both callers see identical device behavior
# instead of the push-to-talk mic silently using a different device than
# whatever the user tested/picked in Settings.

import numpy as np
import sounddevice as sd
from PyQt6.QtCore import QThread, QSettings, pyqtSignal

SETTINGS_ORG = "StudyWarden"
SETTINGS_APP = "StudyWarden"
SETTINGS_KEY_MIC_DEVICE = "voice_assistant/mic_device_name"

SAMPLE_RATE = 16000  # what faster-whisper expects -- the one thing every caller records at


def list_input_devices():
    """[(index, name), ...] for every real input-capable device
    currently visible to PortAudio -- excludes output-only devices.
    Best-effort: returns [] (never raises) if PortAudio itself can't be
    queried, e.g. no audio subsystem available at all."""
    devices = []
    try:
        for index, info in enumerate(sd.query_devices()):
            if info.get("max_input_channels", 0) > 0:
                devices.append((index, info["name"]))
    except Exception as e:
        print(f"[audio_devices] couldn't list input devices: {e}")
    return devices


def get_saved_device_name():
    return QSettings(SETTINGS_ORG, SETTINGS_APP).value(SETTINGS_KEY_MIC_DEVICE) or None


def save_device_name(name):
    QSettings(SETTINGS_ORG, SETTINGS_APP).setValue(SETTINGS_KEY_MIC_DEVICE, name or "")


def resolve_device_index(name):
    """Looks up the CURRENT PortAudio index for a device saved by NAME --
    indices aren't stable across reboots/device (re)plugs, so the name is
    what's actually persisted (see save_device_name()); this re-resolves
    it fresh every time a recording actually starts. None (falls back to
    PortAudio's system-default input device) if no name was ever saved,
    or the saved device isn't currently present."""
    if not name:
        return None
    for index, device_name in list_input_devices():
        if device_name == name:
            return index
    return None


class MicRecorderThread(QThread):
    """Captures mic audio from a specific device (device_index=None means
    the system default) into an in-memory buffer until stop() is called.

    level_updated fires with each chunk's peak amplitude (0.0-1.0) AS IT'S
    CAPTURED, not just once at the end -- so a caller can show a live
    meter WHILE recording is still in progress. This is what makes "am I
    actually being heard right now" answerable in the moment, rather than
    only after transcription quietly comes back empty.

    Shared by the real push-to-talk capture (ui/voice_assistant_panel.py)
    and the Settings "Test Mic" feature (ui/settings_dialog.py), so both
    exercise the exact same recording path for whatever device is
    currently selected -- a device that tests fine in Settings is
    guaranteed to behave identically during a real session."""
    level_updated = pyqtSignal(float)
    finished_recording = pyqtSignal(object)  # np.ndarray (float32, mono, SAMPLE_RATE) or None on failure

    def __init__(self, device_index=None, parent=None):
        super().__init__(parent)
        self.device_index = device_index
        self._chunks = []
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True

    def run(self):
        self._chunks = []
        try:
            def _callback(indata, frames, time_info, status):
                chunk = indata.copy()
                self._chunks.append(chunk)
                peak = float(np.abs(chunk).max()) if len(chunk) else 0.0
                self.level_updated.emit(min(1.0, peak))

            with sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                device=self.device_index, callback=_callback,
            ):
                while not self._stop_requested:
                    self.msleep(50)
        except Exception as e:
            print(f"[audio_devices] recording failed (device_index={self.device_index}): {e}")
            self.finished_recording.emit(None)
            return

        if not self._chunks:
            self.finished_recording.emit(None)
            return
        audio = np.concatenate(self._chunks, axis=0).flatten()
        self.finished_recording.emit(audio)
