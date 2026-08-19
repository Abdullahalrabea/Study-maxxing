# Per-application volume control for Spotify via Windows Core Audio
# (pycaw/comtypes) -- a SEPARATE mechanism from spotify_now_playing.py's
# SMTC-based transport controls/metadata, since SMTC has no concept of
# volume at all.
#
# pycaw's own AudioUtilities.GetAllSessions()/GetAudioSessionManager()
# only ever check the CURRENT DEFAULT render device -- confirmed in
# practice on a real machine that this misses Spotify entirely when
# it's routed to a non-default output (here: a SteelSeries Sonar
# virtual audio channel, a common setup for anyone using per-app audio
# routing software). This searches every device's OWN session manager
# instead (AudioDevice.AudioSessionManager -- each device's own
# IAudioSessionManager2), not the default-only static helper.
#
# Verified for real: this works fine from inside a QThread (same check
# already done for winsdk/SMTC -- see spotify_now_playing.py's module
# docstring for why that was worth confirming rather than assuming,
# given pyttsx3's unrelated COM-threading failure earlier this
# session).

import warnings

from PyQt6.QtCore import QThread, pyqtSignal

SPOTIFY_PROCESS_NAME_SUBSTRING = "spotify"


def _find_spotify_session():
    warnings.filterwarnings("ignore")  # pycaw warns on unreadable properties for some virtual/inactive
                                        # devices while enumerating -- noise, not a real failure here
    from pycaw.pycaw import AudioUtilities, IAudioSessionControl2, AudioSession

    for device in AudioUtilities.GetAllDevices():
        try:
            manager = device.AudioSessionManager
            if manager is None:
                continue
            enumerator = manager.GetSessionEnumerator()
            for i in range(enumerator.GetCount()):
                ctl = enumerator.GetSession(i)
                if ctl is None:
                    continue
                ctl2 = ctl.QueryInterface(IAudioSessionControl2)
                if ctl2 is None:
                    continue
                session = AudioSession(ctl2)
                proc = session.Process
                if proc and SPOTIFY_PROCESS_NAME_SUBSTRING in proc.name().lower():
                    return session
        except Exception:
            continue
    return None


def get_volume():
    """Returns Spotify's current per-app volume (0.0-1.0), or None if
    Spotify isn't running / has no active audio session right now
    (never raises)."""
    session = _find_spotify_session()
    if session is None:
        return None
    try:
        return session.SimpleAudioVolume.GetMasterVolume()
    except Exception as e:
        print(f"[spotify_volume] couldn't read volume: {e}")
        return None


def set_volume(level):
    """level: 0.0-1.0. Returns True on success, False if Spotify isn't
    running / has no active session, or the call otherwise fails (never
    raises)."""
    session = _find_spotify_session()
    if session is None:
        return False
    try:
        session.SimpleAudioVolume.SetMasterVolume(max(0.0, min(1.0, level)), None)
        return True
    except Exception as e:
        print(f"[spotify_volume] couldn't set volume: {e}")
        return False


class VolumeReadWorker(QThread):
    done = pyqtSignal(object)  # float 0.0-1.0, or None

    def run(self):
        self.done.emit(get_volume())


class VolumeWriteWorker(QThread):
    done = pyqtSignal(bool)

    def __init__(self, level, parent=None):
        super().__init__(parent)
        self.level = level

    def run(self):
        self.done.emit(set_volume(self.level))
