# In-app self-update: checks the latest GitHub Release, downloads the
# published onedir build, and swaps it into place via a small generated
# PowerShell script (see UpdateApplyWorker._write_swap_script()) -- a
# near-atomic pair of directory renames, run detached AFTER this process
# has already quit, since Windows won't let a running exe's own
# directory be replaced out from under it.
#
# Repo is public, so every request here is unauthenticated -- GitHub
# requires a real User-Agent header on API requests or it 403s.

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import requests
from PyQt6.QtCore import QThread, pyqtSignal

from paths import get_app_data_dir, get_current_version

GITHUB_OWNER = "Abdullahalrabea"
GITHUB_REPO = "Study-maxxing"
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
REQUEST_TIMEOUT_SEC = 15
DOWNLOAD_TIMEOUT_SEC = 300
USER_AGENT = "StudyMaxxing-UpdateChecker"

# Matches the asset name Phase E's release process attaches, e.g.
# "StudyWarden-v0.1.0-win64.zip" -- see the plan's Phase E notes.
ASSET_NAME_PATTERN = re.compile(r"^StudyWarden-v[\d.]+-win64\.zip$")


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

        latest = release.get("tag_name", "")
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
    """Downloads the release zip, verifies it, extracts it to a sibling
    staging folder, and hands off to a generated PowerShell script that
    does the actual directory swap -- all of which has to happen from a
    SEPARATE process that outlives this one, since Windows won't let a
    running exe's own directory be replaced while it's still running.
    This worker's job ends at "script launched"; `done` means "about to
    quit", not "update finished" -- see _write_swap_script()'s docstring
    for the rest of the story.
    progress: (stage) -- short human-readable status text
    done: () -- staging + script launch succeeded, caller should quit now
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
            zip_path = updates_dir / self.asset["name"]

            self.progress.emit("Downloading...")
            self._download(self.asset["browser_download_url"], zip_path, self.asset["size"])

            install_dir = Path(sys.executable).resolve().parent
            staging_dir = install_dir.parent / (install_dir.name + ".new")
            if staging_dir.exists():
                shutil.rmtree(staging_dir)

            self.progress.emit("Extracting...")
            self._extract(zip_path, staging_dir)

            if not (staging_dir / "StudyWarden.exe").exists() or not (staging_dir / "VERSION").exists():
                raise RuntimeError("Downloaded update is missing StudyWarden.exe or VERSION -- not applying it.")

            self.progress.emit("Preparing to restart...")
            script_path = self._write_swap_script(install_dir, staging_dir)
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            zip_path.unlink(missing_ok=True)
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

    def _extract(self, zip_path, staging_dir):
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(staging_dir.parent)
        # The release zip contains a single top-level "StudyWarden/" folder
        # (Phase E zips dist/StudyWarden as-is) -- rename that extracted
        # folder to our staging name rather than assuming it already
        # matches (it won't, the first time: "StudyWarden" != "StudyWarden.new").
        extracted = staging_dir.parent / "StudyWarden"
        if extracted != staging_dir:
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            extracted.rename(staging_dir)

    def _write_swap_script(self, install_dir, staging_dir):
        """Waits for this process to exit, then does two directory renames
        (old install -> .old, staging -> live) -- near-atomic, so there's
        never a moment where the install directory is half-old/half-new.
        Rolls back (renames .old back) if the swap fails partway, rather
        than leaving a broken/missing install. Relaunches the new exe and
        cleans up the .old copy once done. Every step logs to
        update_log.txt (in get_app_data_dir(), so it survives the swap
        it's describing) for troubleshooting."""
        pid = os.getpid()
        old_dir = install_dir.parent / (install_dir.name + ".old")
        log_path = get_app_data_dir() / "update_log.txt"
        exe_name = "StudyWarden.exe"

        script = f'''$ErrorActionPreference = "Stop"
$logPath = "{log_path}"
function Log($msg) {{ Add-Content -Path $logPath -Value "$(Get-Date -Format o)  $msg" }}

Log "Waiting for PID {pid} to exit..."
try {{ Wait-Process -Id {pid} -Timeout 30 -ErrorAction SilentlyContinue }} catch {{}}
Start-Sleep -Seconds 1

$installDir = "{install_dir}"
$stagingDir = "{staging_dir}"
$oldDir = "{old_dir}"

try {{
    if (Test-Path $oldDir) {{ Remove-Item -Recurse -Force $oldDir }}
    Log "Renaming $installDir -> $oldDir"
    Rename-Item -Path $installDir -NewName (Split-Path $oldDir -Leaf)
    Log "Renaming $stagingDir -> $installDir"
    Rename-Item -Path $stagingDir -NewName (Split-Path $installDir -Leaf)
    Log "Swap succeeded, relaunching"
    Start-Process -FilePath (Join-Path $installDir "{exe_name}")
    Start-Sleep -Seconds 2
    Remove-Item -Recurse -Force $oldDir -ErrorAction SilentlyContinue
    Log "Cleanup done."
}} catch {{
    Log "Swap FAILED: $_"
    if ((Test-Path $oldDir) -and -not (Test-Path $installDir)) {{
        Log "Rolling back: $oldDir -> $installDir"
        Rename-Item -Path $oldDir -NewName (Split-Path $installDir -Leaf)
        Start-Process -FilePath (Join-Path $installDir "{exe_name}")
    }}
}}
'''
        fd, path = tempfile.mkstemp(suffix=".ps1", prefix="studywarden_update_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
        return Path(path)
