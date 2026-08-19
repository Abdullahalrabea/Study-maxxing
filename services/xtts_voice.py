# Voice cloning via Coqui's XTTS-v2 (community-maintained `coqui-tts`
# fork -- the original Coqui TTS company shut down; this fork keeps
# XTTS-v2 working on current Python). An ALTERNATIVE TTS backend to the
# plain SAPI5 voice already used by default (see ui/voice_assistant_
# panel.py) -- clones a voice from a short REFERENCE AUDIO CLIP you
# provide (AI/Voices/*.mp3), rather than picking from a fixed list of
# pre-baked system voices. User-confirmed personal, non-commercial use
# with reference clips of their own voice.
#
# Model download + license: the first real synthesis call downloads the
# XTTS-v2 checkpoint (~1.8GB) and requires accepting Coqui's Public
# Model License (CPML, non-commercial) -- COQUI_TOS_AGREED=1 accepts
# this without an interactive prompt (there's no console to prompt in
# a GUI app anyway).
#
# Much heavier than SAPI5: a real neural network, with real GPU/CPU
# inference time per utterance (seconds, not near-instant) and a
# multi-GB model kept resident in memory once loaded. Loaded ONCE and
# cached at class level (see XTTSEngine._get_tts()), never reloaded
# per utterance -- the load itself is the slow part.

import os
from pathlib import Path

from PyQt6.QtCore import QSettings, QThread, pyqtSignal

os.environ.setdefault("COQUI_TOS_AGREED", "1")

VOICES_DIR = Path(__file__).resolve().parent.parent / "AI" / "Voices"
MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"
AUDIO_EXTENSIONS = (".mp3", ".wav", ".flac", ".ogg", ".m4a")

SETTINGS_ORG = "StudyWarden"
SETTINGS_APP = "StudyWarden"
SETTINGS_KEY_VOICE_NAME = "voice_assistant/tts_voice_name"  # "" or missing = the default SAPI5 voice


def list_voices():
    """[(name, path), ...] for every audio file in AI/Voices/ -- each
    is a usable reference clip for cloning. `name` is just the
    filename stem (e.g. "master", "peter"), used as the voice picker's
    display label. [] if the folder doesn't exist or is empty."""
    if not VOICES_DIR.exists():
        return []
    return [
        (path.stem, str(path))
        for path in sorted(VOICES_DIR.iterdir())
        if path.suffix.lower() in AUDIO_EXTENSIONS
    ]


def get_saved_voice_name():
    return QSettings(SETTINGS_ORG, SETTINGS_APP).value(SETTINGS_KEY_VOICE_NAME) or None


def save_voice_name(name):
    QSettings(SETTINGS_ORG, SETTINGS_APP).setValue(SETTINGS_KEY_VOICE_NAME, name or "")


def resolve_voice_path(name):
    """None (falls back to the default SAPI5 voice -- see ui/voice_
    assistant_panel.py's _speak()) if no name was ever saved, or the
    saved voice's reference clip is no longer present in AI/Voices/."""
    if not name:
        return None
    for voice_name, path in list_voices():
        if voice_name == name:
            return path
    return None


class XTTSEngine:
    _tts = None

    @classmethod
    def _get_tts(cls):
        if cls._tts is None:
            import torch
            from TTS.api import TTS
            device = "cuda" if torch.cuda.is_available() else "cpu"
            cls._tts = TTS(MODEL_NAME).to(device)
        return cls._tts

    @classmethod
    def synthesize(cls, text, speaker_wav_path, language="en"):
        """Generates speech for `text` cloned from speaker_wav_path
        (one of list_voices()'s paths). Returns (audio_samples,
        sample_rate) -- a plain numpy float array and its real sample
        rate, ready for sounddevice.play(), not a file on disk. Real
        inference: can take several seconds, especially on CPU."""
        tts = cls._get_tts()
        wav = tts.tts(text=text, speaker_wav=speaker_wav_path, language=language)
        import numpy as np
        return np.array(wav, dtype="float32"), tts.synthesizer.output_sample_rate


class XTTSSpeakThread(QThread):
    """Synthesizes `text` in the voice cloned from speaker_wav_path and
    plays it -- shared by both the real voice assistant (see ui/voice_
    assistant_panel.py's _speak()) and Settings' "Preview Voice" button
    (see ui/settings_dialog.py), so both exercise the identical
    synthesis+playback path.

    Runs entirely in-process (no subprocess, unlike the default SAPI5
    path's _TTSThread) -- XTTS-v2 is a pure PyTorch model with no COM/
    SAPI involvement at all, so it doesn't hit the COM-threading wall
    pyttsx3 did (confirmed empirically before committing to this,
    same as every other "does this actually work in a QThread" check
    this session has done rather than assuming).

    Playback is sounddevice.play()/stop() (already a dependency, used
    for mic capture too) -- non-blocking and genuinely interruptible
    mid-utterance. Interrupting DURING synthesis (before any audio
    exists yet) can't stop the neural network's forward pass itself --
    there's no clean cancellation hook for that -- so stop() just marks
    a "cancelled" flag; if synthesis finishes after that, the result is
    discarded instead of played, rather than blocking or crashing."""
    finished_speaking = pyqtSignal()

    def __init__(self, text, speaker_wav_path, parent=None):
        super().__init__(parent)
        self.text = text
        self.speaker_wav_path = speaker_wav_path
        self._cancelled = False

    def run(self):
        import sounddevice as sd
        try:
            audio, sample_rate = XTTSEngine.synthesize(self.text, self.speaker_wav_path)
            if self._cancelled:
                return
            sd.play(audio, sample_rate)
            sd.wait()
        except Exception as e:
            print(f"[xtts_voice] synthesis/playback failed: {e}")
        finally:
            self.finished_speaking.emit()

    def stop(self):
        self._cancelled = True
        import sounddevice as sd
        sd.stop()
