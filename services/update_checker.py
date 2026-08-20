# In-app self-update: checks the latest GitHub Release, downloads the
# published installer, and re-runs it silently -- Inno Setup's own
# Restart Manager integration (CloseApplications/RestartApplications in
# installer.iss) detects that StudyWarden.exe is running, closes it,
# installs over it, and relaunches it. No custom directory-swap
# scripting needed the way updating a plain zip release would require;
# the installer already knows how to replace a running install of
# itself, since that's exactly what upgrade-over-existing-install means.
#
# Repo is public, so every request here is unauthenticated -- GitHub
# requires a real User-Agent header on API requests or it 403s.

import json
import re
import subprocess
import sys

import requests
from PyQt6.QtCore import QThread, pyqtSignal

from paths import get_app_data_dir, get_current_version

GITHUB_OWNER = "Abdullahalrabea"
GITHUB_REPO = "Study-maxxing"
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
REQUEST_TIMEOUT_SEC = 15
DOWNLOAD_TIMEOUT_SEC = 300
USER_AGENT = "StudyMaxxing-UpdateChecker"

# Matches installer.iss's OutputBaseFilename, e.g. "StudyWarden-Setup-v0.1.0.exe".
ASSET_NAME_PATTERN = re.compile(r"^StudyWarden-Setup-v[\d.]+\.exe$")


def _parse_version(text):
    """'v1.2.3' or '1.2.3' -> (1, 2, 3), for real numeric comparison --
    plain string comparison would wrongly sort '10.0.0' before '9.0.0'."""
    text = text.strip().lstrip("vV")
    parts = [p for p in text.split(".") if p.isdigit()]
    if not parts:
        raise ValueError(f"Not a version string: {text!r}")
    return tuple(int(p) for p in parts)


class UpdateCheckWorker(QThread):
    """Mirrors ui/settings_dialog.py's ConnectionTestWorker shape -- a
    single GitHub API call off the GUI thread, never raises.
    done: (update_available, latest_version, message, asset)
    `asset` is the raw GitHub release asset dict (browser_download_url,
    size, name), needed by UpdateApplyWorker below; None unless an
    update is actually available."""
    done = pyqtSignal(bool, str, str, object)

    def run(self):
        current = get_current_version()
        try:
            resp = requests.get(
                RELEASES_API_URL,
                headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
                timeout=REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            release = resp.json()
        except requests.RequestException as e:
            self.done.emit(False, current, f"Couldn't check for updates: {e}", None)
            return
        except json.JSONDecodeError as e:
            self.done.emit(False, current, f"Unexpected response from GitHub: {e}", None)
            return

        # Strip any leading "v"/"V" right here so `latest` is always a bare
        # version number from this point on, same as `current` (from
        # get_current_version(), which never has one) -- the git tag
        # itself is "v0.1.0", and every message below adds its own "v"
        # prefix when displaying either one; without stripping it first,
        # tag-derived messages read as "vv0.1.0" (confirmed the hard way
        # in a real self-update test, not a hypothetical).
        latest = release.get("tag_name", "").strip().lstrip("vV")
        try:
            is_newer = _parse_version(latest) > _parse_version(current)
        except ValueError:
            self.done.emit(False, current, f"Couldn't parse version '{latest}'.", None)
            return

        if not is_newer:
            self.done.emit(False, current, f"You're up to date (v{current}).", None)
            return

        asset = next(
            (a for a in release.get("assets", []) if ASSET_NAME_PATTERN.match(a.get("name", ""))),
            None,
        )
        if asset is None:
            self.done.emit(False, current, f"v{latest} is out, but no matching download was found.", None)
            return

        self.done.emit(True, latest, f"v{latest} is available (you have v{current}).", asset)


class UpdateApplyWorker(QThread):
    """Downloads the release installer and launches it silently, detached
    from this process. This worker's job ends at "installer launched";
    `done` means "about to quit", not "update finished" -- the installer
    itself (running as a separate process) is what actually waits for
    this app to exit, overwrites the install directory, and relaunches
    it, via Inno Setup's Restart Manager integration (see installer.iss).
    progress: (stage) -- short human-readable status text
    done: () -- installer launched successfully, caller should quit now
    failed: (message)"""
    progress = pyqtSignal(str)
    done = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, asset, parent=None):
        super().__init__(parent)
        self.asset = asset

    def run(self):
        if not getattr(sys, "frozen", False):
            self.failed.emit("Self-update only works in a packaged build, not when running from source.")
            return

        try:
            updates_dir = get_app_data_dir() / "updates"
            updates_dir.mkdir(parents=True, exist_ok=True)
            installer_path = updates_dir / self.asset["name"]

            self.progress.emit("Downloading...")
            self._download(self.asset["browser_download_url"], installer_path, self.asset["size"])

            self.progress.emit("Launching installer...")
            # /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS make the automatic
            # close-then-relaunch behavior explicit for a silent run (it's
            # already the [Setup]-section default, see installer.iss, but
            # naming it here removes any doubt this is the mechanism doing
            # the work, not something relying on default flag values).
            subprocess.Popen(
                [
                    str(installer_path),
                    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                    "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS",
                ],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            self.done.emit()
        except Exception as e:
            self.failed.emit(str(e))

    def _download(self, url, dest_path, expected_size):
        with requests.get(url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=DOWNLOAD_TIMEOUT_SEC) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
        actual_size = dest_path.stat().st_size
        if actual_size != expected_size:
            dest_path.unlink(missing_ok=True)
            raise RuntimeError(f"Download incomplete: got {actual_size} bytes, expected {expected_size}.")
