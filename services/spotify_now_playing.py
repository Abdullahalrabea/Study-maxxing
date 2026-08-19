# Reads Spotify's currently-playing track (title/artist/album/cover art)
# directly from Windows' own System Media Transport Controls (SMTC) --
# the same OS-level session info that powers the volume flyout's media
# controls and the lock screen's now-playing widget. No Spotify Developer
# app, API key, or OAuth needed at all: this just reads whatever
# Spotify's desktop app already publishes to the OS, the same way any
# properly-integrated Windows media app does. Windows-only (winsdk wraps
# the WinRT APIs) -- matches this app's existing Windows-only
# assumptions (SAPI5 TTS, DPAPI, GetAsyncKeyState hotkeys, etc.).
#
# Verified for real in this environment: winsdk's WinRT bridge works
# fine from inside a QThread (unlike pyttsx3 -- see voice_assistant_
# panel.py's _TTSThread for that saga), so NowPlayingWorker below calls
# it directly rather than needing a separate-process workaround.

import asyncio

from PyQt6.QtCore import QThread, pyqtSignal

SPOTIFY_APP_ID_SUBSTRING = "Spotify"


async def _fetch_now_playing():
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
    from winsdk.windows.storage.streams import DataReader, Buffer, InputStreamOptions

    manager = await SessionManager.request_async()
    spotify_session = None
    for session in manager.get_sessions():
        if SPOTIFY_APP_ID_SUBSTRING in session.source_app_user_model_id:
            spotify_session = session
            break

    if spotify_session is None:
        return None  # Spotify isn't running, or has no active media session right now

    info = await spotify_session.try_get_media_properties_async()
    playback = spotify_session.get_playback_info()
    is_playing = playback.playback_status == PlaybackStatus.PLAYING

    cover_bytes = None
    if info.thumbnail is not None:
        try:
            stream = await info.thumbnail.open_read_async()
            size = stream.size
            buffer = Buffer(size)
            await stream.read_async(buffer, size, InputStreamOptions.READ_AHEAD)
            reader = DataReader.from_buffer(buffer)
            raw = bytearray(size)
            reader.read_bytes(raw)
            cover_bytes = bytes(raw)
        except Exception:
            cover_bytes = None  # cover art is a nice-to-have, not worth failing the whole read over

    return {
        "title": info.title or "",
        "artist": info.artist or "",
        "album": info.album_title or "",
        "is_playing": is_playing,
        "cover_bytes": cover_bytes,
    }


def fetch_now_playing():
    """Synchronous wrapper -- runs the async WinRT calls to completion.
    Returns None if Spotify isn't running / has no active session, or on
    any error (winsdk missing, SMTC unsupported, etc.) -- the caller
    shows an idle state rather than breaking."""
    try:
        return asyncio.run(_fetch_now_playing())
    except Exception as e:
        print(f"[spotify_now_playing] couldn't read Spotify's session: {e}")
        return None


class NowPlayingWorker(QThread):
    """Runs fetch_now_playing() off the GUI thread -- asyncio.run() spins
    up its own event loop, which would otherwise block the GUI while the
    WinRT round-trip (and optional cover-art download) completes."""
    done = pyqtSignal(object)  # dict (see fetch_now_playing()) or None

    def run(self):
        self.done.emit(fetch_now_playing())


# ---- transport controls: play/pause, next, previous ----
#
# The SAME SMTC session object used for reading now-playing info also
# accepts transport commands -- this is the identical mechanism a
# keyboard's hardware media keys (or the volume flyout's own transport
# buttons) use to control whichever app currently owns the system's
# media session, so it works without any Spotify-specific integration.

TOGGLE_PLAY_PAUSE = "toggle_play_pause"
NEXT = "next"
PREVIOUS = "previous"


async def _send_transport_command(command):
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )

    manager = await SessionManager.request_async()
    spotify_session = None
    for session in manager.get_sessions():
        if SPOTIFY_APP_ID_SUBSTRING in session.source_app_user_model_id:
            spotify_session = session
            break

    if spotify_session is None:
        return False

    if command == TOGGLE_PLAY_PAUSE:
        return await spotify_session.try_toggle_play_pause_async()
    elif command == NEXT:
        return await spotify_session.try_skip_next_async()
    elif command == PREVIOUS:
        return await spotify_session.try_skip_previous_async()
    return False


def send_transport_command(command):
    """Returns True if Spotify accepted the command, False if Spotify
    isn't running / has no active session, or the call otherwise fails
    (never raises)."""
    try:
        return asyncio.run(_send_transport_command(command))
    except Exception as e:
        print(f"[spotify_now_playing] couldn't send {command!r}: {e}")
        return False


class TransportCommandWorker(QThread):
    """Runs send_transport_command() off the GUI thread. done carries
    the command back alongside the result so a caller juggling multiple
    in-flight button presses can tell which one this result belongs
    to."""
    done = pyqtSignal(str, bool)  # (command, succeeded)

    def __init__(self, command, parent=None):
        super().__init__(parent)
        self.command = command

    def run(self):
        self.done.emit(self.command, send_transport_command(self.command))
