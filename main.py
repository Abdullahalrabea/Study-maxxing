# Boots up the full application; will boot when Notion auto-launches Study Warden
import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent
# Every module below uses flat (non-package) imports among its own siblings,
# so each folder needs to be importable on its own rather than as a package.
for folder in ("core", "ui", "ui/games", "services", "AI", "vision"):
    sys.path.insert(0, str(ROOT / folder))

from main_window import MainWindow  # noqa: E402
import theme  # noqa: E402

# Without this, Windows groups a plain `python main.py` process under
# python.exe's own taskbar icon (the Python logo) instead of ours, no
# matter what QApplication.setWindowIcon() is given -- has to be set before
# QApplication exists, and only works on Windows (no-op elsewhere).
if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("StudyWarden.App")
    except Exception:
        pass


def main():
    app = QApplication(sys.argv)
    theme.manager.apply_current()
    # Keeps a reference on the QApplication itself -- an event filter with
    # no surviving Python reference gets garbage-collected and silently
    # stops firing.
    app._button_press_animator = theme.install_button_press_feedback(app)

    cursor = theme.load_pointer_cursor()
    if cursor is not None:
        # A per-window DEFAULT (see install_default_cursor()), not
        # setOverrideCursor() -- an override would force this cursor
        # everywhere, ignoring what every widget itself wants (a
        # QLineEdit's I-beam, a resize handle's arrows, any button with
        # its own setCursor() call), which isn't what "should also
        # change if need be" means.
        app._cursor_installer = theme.install_default_cursor(app, cursor)

    icon = theme.load_app_icon()
    if icon is not None:
        app.setWindowIcon(icon)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
