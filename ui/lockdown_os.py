# Windows-specific OS integration for Monitor Lockdown: multi-monitor
# placement, launching/repositioning Discord + Spotify, minimizing every
# other window (re-enforced periodically -- "soft" lockdown, not a keyboard
# hook), and a Ctrl+Shift+Alt exit detector that works no matter which
# window currently has focus.
#
# Deliberately NOT a low-level keyboard hook (WH_KEYBOARD_LL). A hook that
# swallows Alt+Tab/the Windows key system-wide is meaningfully riskier: a
# bug in it can leave the OS unable to task-switch until the process is
# killed or the machine is restarted, and it's the same technique malicious
# "lockout" tools use. Everything here uses safe, standard, non-blocking
# Win32 APIs instead:
#   - GetAsyncKeyState to detect the exit combo (a read-only "is this key
#     currently down" poll -- it can't intercept or block anything)
#   - ShowWindow/SetWindowPos to minimize/move windows (normal window
#     management, the same thing clicking a taskbar button does)
# So this is "hard to tab away from because it keeps getting minimized back
# down", not "physically impossible to tab away from".

import ctypes
import os
import subprocess
import time
from ctypes import wintypes

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication

user32 = ctypes.windll.user32

SW_MINIMIZE = 6
SW_RESTORE = 9
SWP_NOZORDER = 0x0004

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12  # Alt
VK_T = 0x54

DISCORD_UPDATE_EXE = os.path.expandvars(r"%LOCALAPPDATA%\Discord\Update.exe")
DISCORD_LAUNCH_ARGS = ["--processStart", "Discord.exe"]
SPOTIFY_EXE = os.path.expandvars(r"%APPDATA%\Spotify\Spotify.exe")


def _enum_top_level_windows():
    """Returns [(hwnd, title), ...] for every visible top-level window with
    a non-empty title (skips invisible helper/tool windows)."""
    results = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if buffer.value:
            results.append((hwnd, buffer.value))
        return True

    user32.EnumWindows(_callback, 0)
    return results


def find_window(title_substring):
    """First visible top-level window whose title contains title_substring
    (case-insensitive). Returns an hwnd, or None. Read-only -- safe to call
    any time."""
    needle = title_substring.lower()
    for hwnd, title in _enum_top_level_windows():
        if needle in title.lower():
            return hwnd
    return None


def minimize_window(hwnd):
    user32.ShowWindow(hwnd, SW_MINIMIZE)


def move_window(hwnd, x, y, width, height):
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetWindowPos(hwnd, 0, x, y, width, height, SWP_NOZORDER)


def minimize_other_windows(allowed_hwnds, allowed_title_substrings=()):
    """Minimizes every visible top-level window except our own (in
    allowed_hwnds) and anything whose title contains one of
    allowed_title_substrings (case-insensitive) -- e.g. "Discord", "Spotify".
    Call this repeatedly (see LockdownEnforcer) rather than once, since a
    newly-opened window after the first pass wouldn't otherwise get caught."""
    lowered = [s.lower() for s in allowed_title_substrings]
    for hwnd, title in _enum_top_level_windows():
        if hwnd in allowed_hwnds:
            continue
        if any(s in title.lower() for s in lowered):
            continue
        minimize_window(hwnd)


def _launch(path, args=None):
    if not os.path.exists(path):
        return False
    try:
        subprocess.Popen([path] + (args or []))
        return True
    except OSError:
        return False


def ensure_discord_running():
    return find_window("Discord") is not None or _launch(DISCORD_UPDATE_EXE, DISCORD_LAUNCH_ARGS)


def ensure_spotify_running():
    return find_window("Spotify") is not None or _launch(SPOTIFY_EXE)


def screen_geometry(index):
    """QRect geometry of the index'th connected monitor (0-based), or None
    if that many monitors aren't connected."""
    screens = QGuiApplication.screens()
    if index >= len(screens):
        return None
    return screens[index].geometry()


def arrange_companion_apps(monitor_index=1, wait_sec=3.0, poll_interval=0.3):
    """Launches Discord/Spotify if they're not already running and tiles
    them side by side on the given monitor (0-based -- monitor_index=1 is
    "monitor 2"). Does nothing if that monitor doesn't exist -- there's no
    second screen to place them on, so they're just left wherever they are.
    Blocks briefly (up to wait_sec) for a freshly-launched app's window to
    actually appear -- run this off the GUI thread (see
    CompanionAppsWorker). Returns None on full success, or a short
    human-readable string describing what didn't work (wrong install path,
    window never appeared, no second monitor, etc.) -- this used to fail
    completely silently, which made a real bug (the caller not being wired
    up at all) indistinguishable from Discord/Spotify just not cooperating."""
    geometry = screen_geometry(monitor_index)
    if geometry is None:
        return f"No monitor {monitor_index + 1} detected -- Discord/Spotify left where they are."

    notes = []
    for name, exe_path in (("Discord", DISCORD_UPDATE_EXE), ("Spotify", SPOTIFY_EXE)):
        if find_window(name) is None and not os.path.exists(exe_path):
            notes.append(f"{name} isn't running and wasn't found at the expected install path ({exe_path})")

    ensure_discord_running()
    ensure_spotify_running()

    deadline = time.time() + wait_sec
    discord_hwnd = spotify_hwnd = None
    while time.time() < deadline and (discord_hwnd is None or spotify_hwnd is None):
        discord_hwnd = discord_hwnd or find_window("Discord")
        spotify_hwnd = spotify_hwnd or find_window("Spotify")
        if discord_hwnd is None or spotify_hwnd is None:
            time.sleep(poll_interval)

    half_width = geometry.width() // 2
    if discord_hwnd is not None:
        move_window(discord_hwnd, geometry.x(), geometry.y(), half_width, geometry.height())
    elif not notes:
        notes.append("Discord's window never appeared in time -- couldn't position it")
    if spotify_hwnd is not None:
        move_window(
            spotify_hwnd, geometry.x() + half_width, geometry.y(),
            geometry.width() - half_width, geometry.height(),
        )
    elif not any(n.startswith("Spotify") for n in notes):
        notes.append("Spotify's window never appeared in time -- couldn't position it")

    return "; ".join(notes) if notes else None


class CompanionAppsWorker(QThread):
    """Runs arrange_companion_apps() off the GUI thread -- it can block for
    a few seconds waiting for a freshly-launched app's window to appear,
    which would otherwise freeze the UI right as lockdown starts."""
    done = pyqtSignal(object)  # the diagnostic string from arrange_companion_apps(), or None on success

    def __init__(self, monitor_index=1):
        super().__init__()
        self.monitor_index = monitor_index
        self.result = None

    def run(self):
        try:
            self.result = arrange_companion_apps(monitor_index=self.monitor_index)
        except Exception as e:
            self.result = f"Couldn't arrange Discord/Spotify: {e}"
        finally:
            self.done.emit(self.result)


def _is_key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


class ExitHotkeyDetector(QObject):
    """Polls for Ctrl+Shift+Alt held down simultaneously -- GetAsyncKeyState
    is a read-only "is this key currently pressed" check, not a hook, so it
    can't block or interfere with anything else. Works regardless of which
    window has focus, which matters since soft lockdown allows brief focus
    loss to other windows. Emits `triggered` once per press (not repeatedly
    while held)."""
    triggered = pyqtSignal()

    def __init__(self, parent=None, poll_ms=100):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(poll_ms)
        self._timer.timeout.connect(self._poll)
        self._was_down = False

    def start(self):
        self._was_down = False
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _poll(self):
        is_down = _is_key_down(VK_CONTROL) and _is_key_down(VK_SHIFT) and _is_key_down(VK_MENU)
        if is_down and not self._was_down:
            self.triggered.emit()
        self._was_down = is_down


class HoldKeyDetector(QObject):
    """Like ExitHotkeyDetector, but for genuine press-and-HOLD-and-release
    of a single key (started/stopped signals, not one triggered-once
    signal) -- used for the voice assistant's push-to-talk (see
    voice_assistant_panel.py). Same GetAsyncKeyState polling, for the
    same reason: works regardless of which widget currently has Qt
    focus, and isn't subject to Qt's own KeyPress/KeyRelease delivery --
    which, in practice, was observed to occasionally desync from the
    real physical key state (T visually registered LISTENING, then
    almost immediately "released" on its own via the Qt event path,
    even while the key was still genuinely held down). Faster poll
    interval than ExitHotkeyDetector's default (30ms vs 100ms) -- a
    rare exit combo can tolerate 100ms of latency, live push-to-talk
    capture should feel instant."""
    started = pyqtSignal()
    stopped = pyqtSignal()

    def __init__(self, vk, parent=None, poll_ms=30):
        super().__init__(parent)
        self._vk = vk
        self._timer = QTimer(self)
        self._timer.setInterval(poll_ms)
        self._timer.timeout.connect(self._poll)
        self._was_down = False

    def start(self):
        self._was_down = False
        self._timer.start()

    def stop(self):
        self._timer.stop()
        if self._was_down:
            # e.g. lockdown ending mid-hold -- the caller shouldn't be
            # left thinking a recording is still in progress
            self._was_down = False
            self.stopped.emit()

    def _poll(self):
        is_down = _is_key_down(self._vk)
        if is_down and not self._was_down:
            self.started.emit()
        elif not is_down and self._was_down:
            self.stopped.emit()
        self._was_down = is_down


class LockdownEnforcer(QObject):
    """Repeatedly minimizes every window except the allow-listed ones, so
    something that gets tabbed to (or newly opened) gets pushed back down
    within one poll interval instead of staying up indefinitely."""

    def __init__(self, get_allowed_hwnds, allowed_title_substrings=("Discord", "Spotify"),
                 poll_ms=2000, parent=None):
        super().__init__(parent)
        self._get_allowed_hwnds = get_allowed_hwnds
        self._allowed_title_substrings = allowed_title_substrings
        self._timer = QTimer(self)
        self._timer.setInterval(poll_ms)
        self._timer.timeout.connect(self._enforce)

    def start(self):
        self._enforce()
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _enforce(self):
        minimize_other_windows(self._get_allowed_hwnds(), self._allowed_title_substrings)
