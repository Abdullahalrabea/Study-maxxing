# Downloads and wires up torch + torchaudio's CUDA build at runtime, for
# the packaged .exe only. These are excluded from the PyInstaller bundle
# entirely (see studywarden.spec) -- torch's CUDA runtime alone is
# ~4.3GB installed, well over GitHub's 2GB release-asset limit, so it
# can't ship in the same zip as the rest of the app. In dev
# (`python main.py`), torch is just a normal pip dependency (see
# requirements.txt) and everything here is a no-op.
#
# Wheels are just zip files -- extracting one directly into a folder and
# adding that folder to sys.path is how PEP 427 wheels are meant to be
# consumed even outside pip. The part that's specific to a FROZEN app is
# that Windows' DLL search order won't find torch's own native DLLs
# (torch/lib/*.dll) unless that directory is explicitly registered via
# os.add_dll_directory() before the first `import torch` -- see
# activate() below, called from services/xtts_voice.py right before its
# own lazy `import torch`.
#
# Pinned to the exact versions requirements.txt already pins (2.5.1+cu121,
# cp312, win_amd64), with a sha256 check against the real published wheel
# hash -- a corrupted or tampered download must never get extracted and
# imported.

import hashlib
import os
import shutil
import sys
import zipfile
from pathlib import Path

import requests
from PyQt6.QtCore import QThread, pyqtSignal

from paths import get_app_data_dir

TORCH_WHEEL_URL = "https://download-r2.pytorch.org/whl/cu121/torch-2.5.1%2Bcu121-cp312-cp312-win_amd64.whl"
TORCH_WHEEL_SHA256 = "473d76257636c66b22cbfac6f616d6b522ef3d3473c13decb1afda22a7b059eb"
TORCHAUDIO_WHEEL_URL = "https://download-r2.pytorch.org/whl/cu121/torchaudio-2.5.1%2Bcu121-cp312-cp312-win_amd64.whl"
TORCHAUDIO_WHEEL_SHA256 = "80400d75da5852bb5491f6259d47a163a00c2d1479ed57d3d95fde205e1b2815"

WHEELS = [
    ("torch", TORCH_WHEEL_URL, TORCH_WHEEL_SHA256),
    ("torchaudio", TORCHAUDIO_WHEEL_URL, TORCHAUDIO_WHEEL_SHA256),
]

DOWNLOAD_TIMEOUT_SEC = 600

# torch's wheel is 2.45GB compressed, ~4.3GB once extracted -- and both
# exist on disk at once mid-extraction (the .whl isn't deleted until
# after), so peak usage is closer to 6.75GB than either number alone.
# Checked with a real margin BEFORE downloading a single byte: a
# %LOCALAPPDATA% that's nearly full (a real, hit-in-testing case, not a
# hypothetical) should fail fast with a clear message, not die 10
# minutes into a download with a bare "No space left on device" OSError,
# or worse, leave a multi-GB half-extracted mess sitting there silently.
REQUIRED_FREE_BYTES = 8 * 1024**3

# Written only after a full download+extract+activate+import cycle
# succeeds -- is_available() checks for THIS, not merely "does torch/
# __init__.py exist", because a failed extraction (disk full, killed
# mid-way, etc.) can leave early wheel members on disk without the rest
# of the package -- checking one shallow file's existence would then
# report "available" for a torch that isn't actually importable.
_READY_MARKER_NAME = ".ready"


def _runtime_dir():
    return get_app_data_dir() / "torch_runtime"


def is_available():
    """True if torch is either a normal pip dependency (dev mode) or
    already downloaded+extracted+verified (frozen mode) -- callers use
    this to decide whether TorchDownloadWorker needs to run at all
    before offering voice cloning."""
    if not getattr(sys, "frozen", False):
        return True
    return (_runtime_dir() / _READY_MARKER_NAME).exists()


def activate():
    """Makes an already-downloaded torch runtime importable: adds its
    directory to sys.path and registers its native DLL folder, so the
    next `import torch` finds it. No-op in dev mode, and safe to call
    even if nothing's been downloaded yet -- also a no-op then.
    Idempotent; call before every lazy `import torch`, not just once."""
    if not getattr(sys, "frozen", False):
        return
    runtime_dir = _runtime_dir()
    runtime_str = str(runtime_dir)
    if runtime_str not in sys.path:
        sys.path.insert(0, runtime_str)
    torch_lib_dir = runtime_dir / "torch" / "lib"
    if torch_lib_dir.exists():
        os.add_dll_directory(str(torch_lib_dir))


class TorchDownloadWorker(QThread):
    """Downloads + extracts the torch/torchaudio wheels into
    get_app_data_dir()/torch_runtime -- a one-time ~2.5GB fetch, only
    ever needed in a packaged build, only ever run once (is_available()
    skips it on every later launch). Streams straight to disk rather
    than buffering in memory, same as ui/settings_dialog.py's
    UpdateApplyWorker.
    progress: (stage, fraction) -- fraction is 0.0-1.0 within the current stage
    done: ()
    failed: (message)"""
    progress = pyqtSignal(str, float)
    done = pyqtSignal()
    failed = pyqtSignal(str)

    def run(self):
        runtime_dir = _runtime_dir()
        try:
            runtime_dir.mkdir(parents=True, exist_ok=True)

            free_bytes = shutil.disk_usage(runtime_dir).free
            if free_bytes < REQUIRED_FREE_BYTES:
                free_gb = free_bytes / 1024**3
                needed_gb = REQUIRED_FREE_BYTES / 1024**3
                raise RuntimeError(
                    f"Not enough free disk space on {runtime_dir.drive or runtime_dir.anchor}: "
                    f"{free_gb:.1f}GB free, need at least {needed_gb:.0f}GB."
                )

            for name, url, expected_sha256 in WHEELS:
                wheel_path = runtime_dir / f"{name}.whl.download"
                self._download(url, wheel_path, expected_sha256, name)
                self.progress.emit(f"Installing {name}...", 1.0)
                with zipfile.ZipFile(wheel_path) as zf:
                    zf.extractall(runtime_dir)
                wheel_path.unlink(missing_ok=True)

            activate()
            self.progress.emit("Verifying...", 1.0)
            import torch  # noqa: F401 -- a real smoke test, not just "files exist"
            (runtime_dir / _READY_MARKER_NAME).write_text("ok", encoding="utf-8")
            self.done.emit()
        except Exception as e:
            # A half-downloaded/half-extracted attempt (disk full,
            # interrupted, corrupt) is worse than useless left on disk --
            # it's multiple GB of debris AND (before the is_available()
            # fix above) could even look "ready" without being
            # importable. Always start a retry from a clean slate.
            shutil.rmtree(runtime_dir, ignore_errors=True)
            self.failed.emit(str(e))

    def _download(self, url, dest_path, expected_sha256, name):
        resp = requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT_SEC)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        hasher = hashlib.sha256()
        downloaded = 0
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                hasher.update(chunk)
                downloaded += len(chunk)
                if total:
                    self.progress.emit(f"Downloading {name}...", downloaded / total)

        if hasher.hexdigest() != expected_sha256:
            dest_path.unlink(missing_ok=True)
            raise RuntimeError(f"{name} download failed checksum verification -- try again.")
