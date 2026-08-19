# -*- mode: python ; coding: utf-8 -*-
#
# Onedir build (not onefile): faster repeat launches, avoids onefile's
# dropper-pattern AV false-positive risk, and is what makes a safe
# directory-swap self-update possible at all (services/update_checker.py).
#
# Build with:  pyinstaller studywarden.spec --noconfirm
# (PYTHONUTF8=1 in the environment -- one pose reference file has an
# emoji in its filename and needs UTF-8 preserved byte-for-byte.)

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

# These packages are all either native-heavy, plugin-loading, or ship
# their own non-Python data files (model configs, WinRT metadata, etc.)
# -- collect_all is the reliable way to get everything PyInstaller's own
# static import scan would otherwise miss.
for pkg in ("mediapipe", "torch", "torchaudio", "TTS", "faster_whisper", "ctranslate2", "winsdk"):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

# win32crypt (core/session_snapshot.py's DPAPI encryption) and pycaw's
# submodule (services/spotify_volume.py) are both only reached via a
# lazily-executed `import` inside a function/try-block deep in the call
# graph -- PyInstaller's static scan follows those fine, but naming them
# here too costs nothing and removes any doubt.
hiddenimports += ["win32crypt", "win32timezone", "pycaw.pycaw"]

# Our own bundled, read-only resources -- must land at these exact
# relative paths since core/paths.py's get_resource_dir() resolves them
# from sys._MEIPASS using this same relative structure at runtime.
datas += [
    ("vision/efficientdet_lite0.tflite", "vision"),
    ("vision/efficientdet_lite2.tflite", "vision"),
    ("vision/face_landmarker.task", "vision"),
    ("vision/hand_landmarker.task", "vision"),
    ("vision/pose_landmarker_lite.task", "vision"),
    ("vision/poses", "vision/poses"),
    ("ui/App Icon", "ui/App Icon"),
    ("ui/cursor", "ui/cursor"),
    ("ui/Images", "ui/Images"),
    ("VERSION", "."),
]
# AI/Voices/* is deliberately NOT bundled here -- those are reference
# clips the user drops in after install (see services/xtts_voice.py),
# not something the app ships. Ships with no default cloned voice.

a = Analysis(
    ["main.py"],
    pathex=["core", "ui", "ui/games", "services", "AI", "vision"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="StudyWarden",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon="ui/App Icon/Pepopolice.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="StudyWarden",
)
