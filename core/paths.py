# Where this app's own local data files live -- one shared answer so every
# file that persists something locally (session_snapshot.py's encrypted
# debt ledger, engagement_log.py/passive_tracking.py's history JSON,
# main_window.py's embedded-Notion browser profile) agrees on the same
# directory, rather than each resolving its own path relative to its own
# __file__ location.
#
# In dev (running from source), this is today's existing behavior
# unchanged: the repo root itself, so a plain `python main.py` still reads/
# writes right next to the project like it always has.
#
# Once packaged (PyInstaller's `sys.frozen`), the repo root becomes the
# INSTALL directory -- writing there is exactly what an in-app self-updater
# can't tolerate, since every update stages a fresh copy of the install and
# swaps it in, which would silently wipe local data on every single update.
# %LOCALAPPDATA%\StudyWarden\ is a real per-user, always-writable directory
# that persists across updates (and across a from-scratch reinstall to a
# different folder) precisely because it's never touched by the swap.

import sys
from pathlib import Path

APP_DATA_DIR_NAME = "StudyWarden"


def get_app_data_dir():
    """The directory this app's own local data files should live in --
    creates it if it doesn't exist yet. Callers build their own filename
    inside this (e.g. get_app_data_dir() / ".session_snapshot.enc")."""
    if getattr(sys, "frozen", False):
        import os
        app_dir = Path(os.environ["LOCALAPPDATA"]) / APP_DATA_DIR_NAME
    else:
        app_dir = Path(__file__).resolve().parent.parent
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


def get_resource_dir(relative_path):
    """Read-only bundled resources (images, cursors, vision models) --
    distinct from get_app_data_dir() above, which is for writable data.

    In dev, this is the repo root joined with `relative_path` (e.g.
    "ui/Images"), same as today's `Path(__file__).resolve().parent /
    "Images"`-style lookups. Once packaged, PyInstaller's bootloader
    extracts/stages bundled files under `sys._MEIPASS` and it's the only
    reliable source for that location -- unlike get_app_data_dir(), this
    directory is never written to, so there's no update-wipe concern and
    no need to route it through %LOCALAPPDATA%.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent.parent
    return base / relative_path
