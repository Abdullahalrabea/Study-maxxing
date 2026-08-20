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
#
# torch/torchaudio are deliberately NOT collected here -- torch's CUDA
# runtime alone is ~4.3GB installed, well over GitHub's 2GB release-asset
# limit. services/torch_runtime.py downloads+wires them up at runtime
# instead (a one-time ~2.5GB fetch, only when voice cloning is actually
# used); see the `excludes` list below, which is what actually keeps them
# out despite TTS.api importing torch itself.
# transformers is also named explicitly (not just pulled in transitively
# via TTS) -- it uses a dynamic lazy-module system (_LazyModule) that
# resolves submodules like transformers.models.gpt2.modeling_gpt2 via
# runtime importlib calls PyInstaller's static bytecode scan can't see
# through, a well-known issue for this library specifically. Confirmed
# the hard way: XTTS-v2's GPT2-based text encoder failed with
# "Could not import module 'GPT2PreTrainedModel'" without this.
# ko_speech_tools and anyascii (both TTS text-cleaning dependencies, see
# TTS/tts/layers/xtts/tokenizer.py and TTS/tts/utils/text/cleaners.py)
# each ship hundreds of small data/resource submodules read via
# importlib.resources/pkgutil at call time, not statically -- collect_all
# ("TTS") doesn't reach into TTS's own dependencies' data files, only
# TTS's. Confirmed ko_speech_tools the hard way ("No module named
# 'ko_speech_tools.data'"); anyascii has the identical pattern
# (anyascii/_data/000, 001, 002, ... loaded via importlib.resources.files())
# so it's named here preemptively rather than waiting to hit it too.
for pkg in ("mediapipe", "TTS", "transformers", "ko_speech_tools", "anyascii", "faster_whisper", "ctranslate2", "winsdk"):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

# Because torch itself is excluded above, PyInstaller's static analysis
# never walks INTO it to discover what stdlib modules it needs -- these
# are real, found by diffing sys.modules before/after `import torch;
# torch.cuda.is_available()` in this exact dev venv (2.5.1+cu121), not a
# guess. Without this, the runtime-downloaded torch (services/
# torch_runtime.py) imports fine up to whichever of these it hits first,
# then fails with a bare "No module named 'x'" -- confirmed the hard way
# (pickletools) before enumerating the rest instead of discovering them
# one rebuild at a time.
hiddenimports += [
    "__future__", "_compat_pickle", "_compression", "argparse", "ast",
    "asyncio", "base64", "bdb", "bisect", "bz2", "calendar", "cmd", "code",
    "codeop", "concurrent", "contextlib", "contextvars", "copy", "copyreg",
    "csv", "ctypes", "dataclasses", "datetime", "difflib", "dis", "email",
    "enum", "fnmatch", "gettext", "glob", "gzip", "hashlib", "heapq",
    "http", "importlib", "inspect", "ipaddress", "json", "linecache",
    "locale", "logging", "lzma", "multiprocessing", "nturl2path",
    "numbers", "opcode", "pathlib", "pdb", "pickle", "pickletools",
    "pkgutil", "platform", "pprint", "queue", "quopri", "random", "re",
    "selectors", "shutil", "signal", "socket", "ssl", "string", "struct",
    "subprocess", "tarfile", "tempfile", "textwrap", "timeit", "token",
    "tokenize", "traceback", "typing", "unittest", "urllib", "uuid",
    "weakref", "zipfile",
]

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
    # Force these out even though TTS.api imports torch itself (PyInstaller's
    # static scan would otherwise re-pull them in despite the collect_all
    # loop above skipping them) -- see that loop's comment for why.
    excludes=["torch", "torchaudio"],
    noarchive=False,
    # inflect (a TTS text-cleaning dependency, TTS/tts/utils/text/english/
    # number_norm.py) decorates its `engine` class with typeguard's
    # @typechecked, which calls inspect.getsource() at IMPORT TIME to
    # instrument the class -- impossible in a frozen build, since there's
    # no .py source on disk to read, only compiled bytecode. typeguard's
    # own typechecked() has a documented bypass for exactly this class of
    # problem though: `if not __debug__: return target` skips
    # instrumentation entirely. optimize=1 (like running `python -O`)
    # sets __debug__ = False, which no other code in this app relies on
    # (grepped for `assert` across our own modules -- none), so this
    # fixes inflect for free rather than needing an inflect version pin
    # or a typeguard monkeypatch.
    optimize=1,
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
