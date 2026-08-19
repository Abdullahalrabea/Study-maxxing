# Visual theme system for Study Warden: a handful of named color themes
# (the original black/green terminal look, a plain light theme, and five
# palette-driven themes), a custom cursor from ui/cursor/, the
# month-timeline header banner (with a settings gear button + a live
# clock), and CollapsibleSection for the toggleable left-side pages.
#
# Theme switching is live: ThemeManager (the module-level `manager`
# singleton) re-applies the QApplication stylesheet and emits `changed` so
# widgets that can't be styled through QSS alone (HeaderBanner's inline
# per-label styles, CollapsibleSection's toggle button) can update their
# colors on the spot instead of requiring an app restart.

import io
from datetime import datetime
from pathlib import Path

from PIL import Image
from PyQt6.QtCore import Qt, QEvent, QTimer, QObject, pyqtSignal, QSettings, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QCursor, QPixmap, QIcon, QColor
from PyQt6.QtWidgets import (
    QWidget, QGridLayout, QLabel, QHBoxLayout, QVBoxLayout, QPushButton, QSizePolicy, QApplication,
    QGraphicsOpacityEffect, QGraphicsDropShadowEffect, QFrame
)

CURSOR_DIR = Path(__file__).resolve().parent / "cursor"
POINTER_CURSOR_PATH = CURSOR_DIR / "Wii Cursor.cur"  # the actual file on disk -- this constant previously
                                                       # pointed at a name ("wii-pointer.cur") that never
                                                       # existed, so load_pointer_cursor() silently returned
                                                       # None every time and the app always fell back to the
                                                       # system default cursor
APP_ICON_PATH = Path(__file__).resolve().parent / "App Icon" / "Pepopolice.jpg"

# Falls back to a system monospace font if Roboto Mono isn't installed --
# Qt silently substitutes rather than erroring, so this is safe either way.
FONT_FAMILY = "Roboto Mono"

# Arc-browser-style redesign: a shared radius scale, theme-independent
# (same shape for every palette in THEMES -- only color comes from there).
# Builds on the only rounding precedents that existed before this: the
# 8px QRadioButton::indicator, HelpIcon's 10px circular badge, the
# settings gear's 18px circle.
RADIUS_TAG = 6        # chips/badges/swatches
RADIUS_INPUT = 8      # QLineEdit/QTextEdit/QDateEdit/QComboBox/QSpinBox
RADIUS_BUTTON = 16    # QPushButton -- paired with more generous padding below
RADIUS_TAB = 12       # QTabBar::tab, CollapsibleSection header's non-spine corners
RADIUS_CARD = 20      # Card container -- the largest tier

# Card's soft shadow -- color comes from the theme's own `border` token at
# low alpha (not a new neutral gray), so each theme's shadow reads as that
# theme's own ambient glow rather than a generic drop shadow bolted on
# alike everywhere. Tuned during the all-9-themes verification pass;
# revisit CARD_SHADOW_ALPHA if a theme's shadow reads as invisible (too
# low against a dark bg) or muddy (too high).
CARD_SHADOW_BLUR = 28
CARD_SHADOW_Y = 6
CARD_SHADOW_ALPHA = 70

SETTINGS_ORG = "StudyWarden"
SETTINGS_APP = "StudyWarden"
SETTINGS_KEY_THEME = "appearance/theme"

DEFAULT_THEME_ID = "terminal_dark"

# Each theme is a full set of color roles a widget might need -- built from
# whichever named palette inspired it (see the "Colors" screenshots this was
# built from), except where a palette had no color dark/light enough for
# legible body text, in which case one neutral color was added for that
# purpose alone (noted per-theme below).
THEMES = {
    "terminal_dark": {
        "label": "Terminal Green (Dark)",
        "bg": "#000000",
        "surface": "#001a00",
        "surface_hover": "#003300",
        "text": "#00FF41",
        "text_dim": "#006600",
        "accent": "#00FF41",
        "border": "#00FF41",
        "selection_bg": "#00FF41",
        "selection_text": "#000000",
    },
    "clean_light": {
        "label": "Clean (Light)",
        "bg": "#FFFFFF",
        "surface": "#F2F2F2",
        "surface_hover": "#E4E4E4",
        "text": "#1A1A1A",
        "text_dim": "#767676",
        "accent": "#0077B6",
        "border": "#C9C9C9",
        "selection_bg": "#0077B6",
        "selection_text": "#FFFFFF",
    },
    "molten_fire": {
        # Molten Lava / Flag Red / Papaya Whip / Deep Space Blue / Steel Blue
        "label": "Molten Fire",
        "bg": "#FDF0D5",
        "surface": "#FFFFFF",
        "surface_hover": "#F6E0BE",
        "text": "#003049",
        "text_dim": "#669BBC",
        "accent": "#C1121F",
        "border": "#780000",
        "selection_bg": "#C1121F",
        "selection_text": "#FDF0D5",
    },
    "ocean_twilight": {
        # Deep Twilight / French Blue / Bright Teal Blue / Blue Green /
        # Turquoise Surf / Sky Aqua / Frosted Blue
        "label": "Ocean Twilight",
        "bg": "#03045E",
        "surface": "#023E8A",
        "surface_hover": "#0077B6",
        "text": "#90E0EF",
        "text_dim": "#48CAE4",
        "accent": "#00B4D8",
        "border": "#0096C7",
        "selection_bg": "#00B4D8",
        "selection_text": "#03045E",
    },
    "forest_sage": {
        # Dust Grey / Dry Sage / Fern / Hunter Green / Pine Teal
        "label": "Forest Sage",
        "bg": "#DAD7CD",
        "surface": "#F2F1EC",
        "surface_hover": "#A3B18A",
        "text": "#344E41",
        "text_dim": "#3A5A40",
        "accent": "#588157",
        "border": "#A3B18A",
        "selection_bg": "#588157",
        "selection_text": "#DAD7CD",
    },
    "blossom": {
        # Soft Blush / Pastel Pink / Cherry Blossom / Cotton Candy / Petal
        # Rouge -- none of these are dark enough for legible body text on a
        # near-white pink background, so text uses one added neutral dark
        # rose (#7A2340) instead of a palette color.
        "label": "Blossom",
        "bg": "#FFE5EC",
        "surface": "#FFFFFF",
        "surface_hover": "#FFC2D1",
        "text": "#7A2340",
        "text_dim": "#FB6F92",
        "accent": "#FB6F92",
        "border": "#FFB3C6",
        "selection_bg": "#FB6F92",
        "selection_text": "#FFFFFF",
    },
    "royal_violet": {
        # Dark Amethyst / Indigo Ink / Indigo Velvet / Royal Violet /
        # Lavender Purple / Mauve Magic / Mauve
        "label": "Royal Violet",
        "bg": "#240046",
        "surface": "#3C096C",
        "surface_hover": "#5A189A",
        "text": "#E0AAFF",
        "text_dim": "#C77DFF",
        "accent": "#9D4EDD",
        "border": "#7B2CBF",
        "selection_bg": "#9D4EDD",
        "selection_text": "#240046",
    },
    "minecraft": {
        # Deepslate / Dirt Brown / Stone Gray / Grass Block Green / Diamond Ore Cyan
        "label": "Minecraft",
        "bg": "#211D1A",
        "surface": "#3E3B37",
        "surface_hover": "#5C4A34",
        "text": "#F5F5F0",
        "text_dim": "#A8A296",
        "accent": "#5FE1D4",
        "border": "#5D8A3A",
        "selection_bg": "#5D8A3A",
        "selection_text": "#211D1A",
    },
    "wii": {
        # Wii Menu Soft Sky / Wii Remote White / Wii Blue / Console Silver --
        # bg is a soft blue-tinted white (not pure white) and the accent is a
        # brighter cyan-blue specifically to stay visually distinct from
        # clean_light's plain white + muted steel blue
        "label": "Nintendo Wii",
        "bg": "#E9EEF2",
        "surface": "#FFFFFF",
        "surface_hover": "#D3E4EF",
        "text": "#1B2A38",
        "text_dim": "#6B818F",
        "accent": "#00A6E4",
        "border": "#A9BFCB",
        "selection_bg": "#00A6E4",
        "selection_text": "#FFFFFF",
    },
}


def build_stylesheet(colors):
    return f"""
QWidget {{
    background-color: {colors['bg']};
    color: {colors['text']};
    font-family: '{FONT_FAMILY}', 'Consolas', monospace;
}}
QFrame#themed-divider {{
    background-color: {colors['border']};
    border: none;
}}
QWidget#TodoRow {{
    background-color: {colors['surface']};
    border-radius: {RADIUS_TAB}px;
}}
Card {{
    background-color: {colors['surface']};
    border: 1px solid {colors['border']};
    border-radius: {RADIUS_CARD}px;
}}
QPushButton {{
    background-color: {colors['surface']};
    border: 2px solid {colors['border']};
    border-radius: {RADIUS_BUTTON}px;
    color: {colors['text']};
    padding: 10px 22px;
    font-weight: bold;
}}
QPushButton:hover {{
    background-color: {colors['surface_hover']};
    border-color: {colors['accent']};
}}
QPushButton:pressed {{
    background-color: {colors['selection_bg']};
    color: {colors['selection_text']};
    border-color: {colors['selection_bg']};
}}
QPushButton:disabled {{
    color: {colors['text_dim']};
    border-color: {colors['text_dim']};
}}
QPushButton:checked {{
    background-color: {colors['surface_hover']};
    border-color: {colors['accent']};
}}
QPushButton:focus, QLineEdit:focus, QTextEdit:focus, QDateEdit:focus, QComboBox:focus, QSpinBox:focus {{
    border: 2px solid {colors['accent']};
}}
QLineEdit, QTextEdit, QDateEdit, QComboBox, QSpinBox {{
    background-color: {colors['surface']};
    border: 2px solid {colors['border']};
    border-radius: {RADIUS_INPUT}px;
    color: {colors['text']};
    padding: 6px 10px;
    selection-background-color: {colors['selection_bg']};
    selection-color: {colors['selection_text']};
}}
QLineEdit:disabled, QTextEdit:disabled, QDateEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    color: {colors['text_dim']};
    border-color: {colors['text_dim']};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
    border-top-right-radius: {RADIUS_INPUT}px;
    border-bottom-right-radius: {RADIUS_INPUT}px;
}}
QComboBox QAbstractItemView {{
    background-color: {colors['surface']};
    color: {colors['text']};
    border: 2px solid {colors['accent']};
    selection-background-color: {colors['selection_bg']};
    selection-color: {colors['selection_text']};
    outline: none;
}}
QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    border-radius: 8px;
    border: 2px solid {colors['border']};
    background-color: {colors['surface']};
}}
QRadioButton::indicator:checked {{
    background-color: {colors['accent']};
    border: 2px solid {colors['accent']};
}}
QTableWidget {{
    background-color: {colors['surface']};
    color: {colors['text']};
    gridline-color: {colors['border']};
    selection-background-color: {colors['selection_bg']};
    selection-color: {colors['selection_text']};
}}
QHeaderView::section {{
    background-color: {colors['bg']};
    color: {colors['accent']};
    border: none;
    border-bottom: 2px solid {colors['accent']};
    padding: 4px 6px;
    font-weight: bold;
}}
QTabWidget::pane {{
    border: 2px solid {colors['border']};
}}
QTabBar::tab {{
    background-color: {colors['bg']};
    border: 2px solid {colors['border']};
    border-top-left-radius: {RADIUS_TAB}px;
    border-top-right-radius: {RADIUS_TAB}px;
    padding: 6px 14px;
    color: {colors['text_dim']};
    font-weight: bold;
}}
QTabBar::tab:selected {{
    background-color: {colors['surface_hover']};
    color: {colors['text']};
    border-color: {colors['accent']};
}}
QScrollBar {{
    background-color: {colors['bg']};
}}
QScrollBar::handle {{
    background-color: {colors['border']};
    border-radius: 5px;
    min-height: 24px;
    min-width: 24px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0px;
    width: 0px;
    border: none;
    background: none;
}}
QGroupBox {{
    border: 2px solid {colors['border']};
    border-top: 3px solid {colors['accent']};
    border-radius: {RADIUS_TAB}px;
    margin-top: 14px;
    padding-top: 14px;
    font-weight: bold;
    font-size: 13px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 6px;
    color: {colors['accent']};
}}
"""


class ThemeManager(QObject):
    """Owns the current theme, persists the choice, and applies it live --
    QApplication-wide via setStyleSheet(), plus a `changed` signal for the
    few widgets (HeaderBanner, CollapsibleSection) that set inline styles
    QSS alone can't reach."""

    changed = pyqtSignal(str, dict)  # (theme_id, colors)

    def __init__(self):
        super().__init__()
        self._settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        saved = self._settings.value(SETTINGS_KEY_THEME)
        self.current_id = saved if saved in THEMES else DEFAULT_THEME_ID
        self.colors = THEMES[self.current_id]

    def apply(self, theme_id):
        if theme_id not in THEMES:
            return
        self.current_id = theme_id
        self.colors = THEMES[theme_id]
        self._settings.setValue(SETTINGS_KEY_THEME, theme_id)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_stylesheet(self.colors))
        self.changed.emit(theme_id, self.colors)

    def apply_current(self):
        """Applies whatever theme was loaded/selected without re-writing
        settings -- used once at startup."""
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_stylesheet(self.colors))


manager = ThemeManager()


class Card(QFrame):
    """Rounded, soft-shadowed container -- the Arc-style building block
    other panels wrap their content in. Background/border/radius come
    for free from build_stylesheet()'s `Card {}` rule, matched by Qt
    class name the same way every other widget's rule is -- instances
    write zero QSS of their own. Only the drop shadow is set here
    imperatively, since QSS has no way to express one at all.

    Two silent Qt gotchas this works around:
    - WA_StyledBackground is required or a QFrame subclass's QSS
      background/border-radius silently doesn't paint at all.
    - Qt never clips children to a parent's rounded corners -- give
      whatever's placed inside a Card real margins (16-20px) so content
      doesn't visibly poke past the rounded edge; Card itself doesn't
      enforce this, since the right margin depends on what's inside."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._shadow = QGraphicsDropShadowEffect(self)
        self.setGraphicsEffect(self._shadow)
        self._apply_shadow(manager.colors)
        manager.changed.connect(self._on_theme_changed)

    def _on_theme_changed(self, theme_id, colors):
        self._apply_shadow(colors)

    def _apply_shadow(self, colors):
        # Shadow color comes from the theme's own `border` token at low
        # alpha rather than a new neutral gray, so it reads as that
        # theme's own ambient glow instead of a generic drop shadow
        # bolted on alike everywhere.
        color = QColor(colors["border"])
        color.setAlpha(CARD_SHADOW_ALPHA)
        self._shadow.setColor(color)
        self._shadow.setBlurRadius(CARD_SHADOW_BLUR)
        self._shadow.setOffset(0, CARD_SHADOW_Y)


PRESS_FADE_MS = 90  # button press-feedback dip -- fast, "instant feedback" tier


class ButtonPressAnimator(QObject):
    """Installed application-wide (see install_button_press_feedback): gives
    every QPushButton a quick opacity dip on press and recovery on release,
    real tactile feedback beyond the :pressed color swap in the stylesheet.
    Opacity, not a scale/geometry animation -- QSS has no transform
    property, and animating geometry directly fights Qt's layout engine for
    any layout-managed widget (i.e. every button in this app)."""

    def eventFilter(self, obj, event):
        if isinstance(obj, QPushButton) and obj.isEnabled():
            if event.type() == QEvent.Type.MouseButtonPress:
                self._animate(obj, 0.55)
            elif event.type() in (QEvent.Type.MouseButtonRelease, QEvent.Type.Leave):
                self._animate(obj, 1.0)
        return False  # never consume -- this only observes

    def _animate(self, button, target_opacity):
        effect = button.graphicsEffect()
        if not isinstance(effect, QGraphicsOpacityEffect):
            effect = QGraphicsOpacityEffect(button)
            effect.setOpacity(1.0)
            button.setGraphicsEffect(effect)
        # Reuse one animation per button (stored on the effect) and redirect
        # it on each new press/release rather than stacking new ones, same
        # pattern as CollapsibleSection's interruptible reveal.
        anim = getattr(effect, "_press_anim", None)
        if anim is None:
            anim = QPropertyAnimation(effect, b"opacity", button)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            effect._press_anim = anim
        anim.stop()
        anim.setDuration(PRESS_FADE_MS)
        anim.setStartValue(effect.opacity())
        anim.setEndValue(target_opacity)
        anim.start()


def install_button_press_feedback(app):
    """Call once at startup with the QApplication instance. Returns the
    animator so the caller can keep a reference alive (an event filter with
    no surviving reference gets garbage-collected and silently stops
    working)."""
    animator = ButtonPressAnimator(app)
    app.installEventFilter(animator)
    return animator


class _DefaultCursorInstaller(QObject):
    """Applies `cursor` as the INHERITED DEFAULT cursor for every top-
    level window the instant it's shown, rather than forcing it
    everywhere. QApplication.setOverrideCursor() was tried first and
    rejected: it's a hard, blanket override that ignores what every
    child widget itself wants, so a QLineEdit's I-beam, a QSplitter
    handle's resize arrow, and any button with its own setCursor()
    call (HeaderBanner's settings gear, HelpIcon, etc.) would all get
    stomped flat to the same custom cursor everywhere -- confirmed via
    a real property-level check that Qt's built-in widgets (QLineEdit
    included) already call setCursor() on THEMSELVES internally
    (WA_SetCursor is True on them without any app code setting it),
    which only takes precedence over an INHERITED default, never over
    an override. Setting the cursor on each window's own top-level
    widget instead lets that normal inheritance keep working exactly
    like the OS's own default arrow-vs-I-beam-vs-resize behavior
    always has -- this cursor just becomes the new baseline, not a
    forced replacement. Installed once on the QApplication so it
    automatically covers every window this app ever opens (lockdown,
    settings, mini-games, dialogs...), not just the main window."""

    def __init__(self, cursor, parent=None):
        super().__init__(parent)
        self._cursor = cursor

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
            obj.setCursor(self._cursor)
        return False


def install_default_cursor(app, cursor):
    """Call once at startup with the QApplication instance and a QCursor
    (see load_pointer_cursor()). Returns the installer so the caller can
    keep a reference alive, same reason as install_button_press_feedback()."""
    installer = _DefaultCursorInstaller(cursor, app)
    app.installEventFilter(installer)
    return installer


def crossfade_theme_change(window, theme_id):
    """Applies a theme with a crossfade illusion instead of the instant
    swap: QSS properties aren't themselves animatable, so this grabs a
    snapshot of `window` in its current look, applies the new theme
    underneath (hidden behind the snapshot), then fades the snapshot away
    to reveal it. `window` should be the top-level window whose look is
    changing (e.g. MainWindow)."""
    if window is None:
        manager.apply(theme_id)
        return

    snapshot = window.grab()
    overlay = QLabel(window)
    overlay.setPixmap(snapshot)
    overlay.setGeometry(window.rect())
    overlay.show()
    overlay.raise_()

    manager.apply(theme_id)  # the actual instant swap, hidden behind the overlay

    effect = QGraphicsOpacityEffect(overlay)
    overlay.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", overlay)
    anim.setDuration(280)
    anim.setStartValue(1.0)
    anim.setEndValue(0.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.finished.connect(overlay.deleteLater)
    overlay._crossfade_anim = anim  # keep it alive for the duration
    anim.start()


def load_pointer_cursor():
    """Loads ui/cursor/wii-pointer.cur into a QCursor, or None if it can't
    be read (missing file, unexpected format) -- callers should fall back
    to the default system cursor in that case rather than fail."""
    if not POINTER_CURSOR_PATH.exists():
        return None
    try:
        image = Image.open(POINTER_CURSOR_PATH).convert("RGBA")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        pixmap = QPixmap()
        pixmap.loadFromData(buffer.getvalue(), "PNG")
        return QCursor(pixmap, 0, 0)
    except Exception:
        return None


def load_app_icon():
    """Loads ui/App Icon/Pepopolice.jpg into a QIcon, or None if it can't
    be read -- callers should just skip setting an icon in that case
    rather than fail."""
    if not APP_ICON_PATH.exists():
        return None
    icon = QIcon(str(APP_ICON_PATH))
    return icon if not icon.isNull() else None


MONTH_LABELS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


class HeaderBanner(QWidget):
    """The month-timeline banner (arrow over the current month) plus a live
    clock and a settings gear button, sitting across the top of the main
    window."""

    settings_requested = pyqtSignal()
    games_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # Hard-capped so this stays a slim strip -- without it, the layout
        # was handing this widget leftover vertical space (314px measured)
        # far beyond what its content actually needs (85px sizeHint),
        # squeezing the pages/Notion area below it.
        self.setMaximumHeight(52)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(10, 2, 10, 2)

        # A fixed SQUARE size + its own font-size override, rather than the
        # global QPushButton rule's padding/font -- previously only width
        # was fixed, so the button came out a squat, mismatched rectangle
        # with the gear glyph tiny and off-center inside it.
        self.settings_button = QPushButton("⚙")  # gear symbol
        self.settings_button.setFixedSize(36, 36)
        self.settings_button.setToolTip("Settings")
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.clicked.connect(self.settings_requested.emit)
        outer.addWidget(self.settings_button)

        # Same square/circular treatment as the gear -- a preview-only
        # way to try a mini-game outside a real break (see
        # ui/overlay.py's GamePreviewDialog/build_game_picker_menu()).
        # Just emits a signal; MainWindow owns the actual menu/dialog so
        # this stays a plain reusable header component that doesn't need
        # to import overlay.py's game roster itself.
        self.games_button = QPushButton("🎮")
        self.games_button.setFixedSize(36, 36)
        self.games_button.setToolTip("Try a mini-game")
        self.games_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.games_button.clicked.connect(self.games_requested.emit)
        outer.addWidget(self.games_button)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(0)
        current_month = datetime.now().month  # 1-12
        self._month_arrows = []
        self._month_labels = []
        for col, name in enumerate(MONTH_LABELS):
            is_current = (col + 1) == current_month

            arrow = QLabel("▼" if is_current else "")
            arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(arrow, 0, col)
            self._month_arrows.append(arrow)

            label = QLabel(name)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setProperty("_is_current", is_current)
            grid.addWidget(label, 1, col)
            self._month_labels.append(label)

        outer.addLayout(grid)
        outer.addStretch()

        self.clock_label = QLabel("")
        outer.addWidget(self.clock_label)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_clock)
        self._timer.start(1000)
        self._update_clock()

        self._apply_colors(manager.colors)
        manager.changed.connect(self._on_theme_changed)

    def _on_theme_changed(self, theme_id, colors):
        self._apply_colors(colors)

    def _apply_colors(self, colors):
        # The current month is the hero moment: a filled accent pill with
        # inverted text, sharply set apart from the other eleven, which
        # recede to a small dim label -- real contrast instead of everything
        # sharing the same weight and color. The clock echoes the same
        # treatment so the header's two "what matters right now" facts read
        # as one deliberate pair, not two unrelated labels.
        accent = colors["accent"]
        dim = colors["text_dim"]
        circular_icon_button_style = (
            f"QPushButton {{ font-size: 20px; padding: 0px; border-radius: 18px; "
            f"background-color: {colors['surface']}; border: 2px solid {colors['border']}; }}"
            f"QPushButton:hover {{ border-color: {accent}; color: {accent}; }}"
            f"QPushButton:pressed {{ background-color: {colors['selection_bg']}; }}"
        )
        self.settings_button.setStyleSheet(circular_icon_button_style)
        self.games_button.setStyleSheet(circular_icon_button_style)
        for arrow in self._month_arrows:
            arrow.setStyleSheet(f"color: {accent}; font-size: 13px; font-weight: bold;")
        for label in self._month_labels:
            if label.property("_is_current"):
                label.setStyleSheet(
                    f"background-color: {accent}; color: {colors['bg']}; "
                    f"font-weight: bold; font-size: 15px; padding: 2px 8px; border-radius: 3px;"
                )
            else:
                label.setStyleSheet(f"color: {dim}; font-weight: normal; font-size: 10px;")
        self.clock_label.setStyleSheet(
            f"font-size: 16px; font-weight: bold; color: {colors['bg']}; "
            f"background-color: {accent}; padding: 3px 10px; border-radius: 3px;"
        )

    def _update_clock(self):
        self.clock_label.setText(datetime.now().strftime("%I:%M:%S %p"))


COLLAPSE_ANIMATION_MS = 220


class HelpIcon(QLabel):
    """A small "?" badge that shows explanatory text as a native tooltip on
    hover -- for "how does this work" text that doesn't need to sit
    permanently in the UI as its own paragraph. Purely informational (not
    clickable) -- QLabel's built-in tooltip-on-hover is all this needs."""

    def __init__(self, text, parent=None):
        super().__init__("?", parent)
        self.setToolTip(text)
        self.setFixedSize(20, 20)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self._apply_colors(manager.colors)
        manager.changed.connect(self._on_theme_changed)

    def _on_theme_changed(self, theme_id, colors):
        self._apply_colors(colors)

    def _apply_colors(self, colors):
        self.setStyleSheet(
            f"QLabel {{ background-color: {colors['bg']}; color: {colors['accent']}; "
            f"border: 2px solid {colors['accent']}; border-radius: 10px; "
            f"font-weight: bold; font-size: 12px; }}"
        )


class CollapsibleSection(QWidget):
    """A titled section with a clickable header that shows/hides its
    content -- used to make the left-side pages toggleable instead of
    tab-switched, so several can be open (or closed) at once. The reveal is
    animated (an exponential-decay ease, like a natural object settling)
    rather than an instant snap -- one deliberate motion moment rather than
    scattered micro-animations everywhere."""

    def __init__(self, title, content_widget, start_expanded=True, help_text=None, parent=None):
        super().__init__(parent)
        self._title = title
        self.content_widget = content_widget
        self._pending_expanded = start_expanded

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # The toggle button and (if given) the help icon share one header
        # strip -- a separate container so both can sit on the same
        # background/border rather than the icon showing the plain window
        # background behind it.
        self.header_container = QWidget()
        header_row = QHBoxLayout(self.header_container)
        header_row.setContentsMargins(0, 0, 8, 0)
        header_row.setSpacing(6)

        self.toggle_button = QPushButton()
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(start_expanded)
        self.toggle_button.clicked.connect(self._on_toggled)
        header_row.addWidget(self.toggle_button, 1)

        self.help_icon = HelpIcon(help_text) if help_text else None
        if self.help_icon is not None:
            header_row.addWidget(self.help_icon)

        layout.addWidget(self.header_container)

        layout.addWidget(self.content_widget)

        self._animation = QPropertyAnimation(self.content_widget, b"maximumHeight", self)
        self._animation.setDuration(COLLAPSE_ANIMATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.finished.connect(self._on_animation_finished)

        self.content_widget.setVisible(start_expanded)
        self.content_widget.setMaximumHeight(16777215 if start_expanded else 0)

        self._update_button_text()
        self._apply_colors(manager.colors)
        manager.changed.connect(self._on_theme_changed)

    def _on_theme_changed(self, theme_id, colors):
        self._apply_colors(colors)

    def _apply_colors(self, colors):
        # A left-side accent spine instead of a plain underline -- an
        # active wayfinding mark for which sections are open, not a
        # decorative half-border on a card.
        self.toggle_button.setStyleSheet(
            f"text-align: left; font-weight: bold; font-size: 16px; "
            f"border: none; border-left: 4px solid {colors['accent']}; "
            f"background-color: {colors['surface']}; padding: 10px 12px; "
            f"border-top-left-radius: 0px; border-bottom-left-radius: 0px; "
            f"border-top-right-radius: {RADIUS_TAB}px; border-bottom-right-radius: {RADIUS_TAB}px;"
        )
        self.header_container.setStyleSheet(f"background-color: {colors['surface']};")

    def _on_toggled(self):
        expanded = self.toggle_button.isChecked()
        self._update_button_text()
        self._animate_to(expanded)

    def _animate_to(self, expanded):
        # setVisible() alone doesn't reliably make an enclosing scroll area
        # reclaim the space, and an instant maximumHeight snap has no
        # motion -- animating maximumHeight from its current pixel height to
        # the real target (0, or the content's actual sizeHint) gives a
        # smooth reveal/collapse that the scroll area's layout re-flows
        # live as each frame changes it.
        self.content_widget.setVisible(True)
        start_height = self.content_widget.height()
        target_height = self.content_widget.sizeHint().height() if expanded else 0

        # QGraphicsDropShadowEffect (used by Card) renders its widget into
        # an offscreen buffer that doesn't reliably stay in sync with
        # rapid animated resizing -- confirmed via real captured frames:
        # button TEXT inside a Card disappeared entirely on several
        # frames mid-animation (background/border still painted fine,
        # just the text glyphs didn't) while the section's maximumHeight
        # was changing every tick. Simplest robust fix: drop every
        # descendant Card's shadow for the animation's duration and give
        # it a fresh one once the height settles -- a shadow blinking off
        # for ~200ms during a reveal nobody's staring that closely at
        # beats a real, visible content glitch.
        self._suspended_cards = self.content_widget.findChildren(Card)
        for card in self._suspended_cards:
            card.setGraphicsEffect(None)

        self._pending_expanded = expanded
        self._animation.stop()
        self._animation.setStartValue(start_height)
        self._animation.setEndValue(target_height)
        self._animation.start()

    def _on_animation_finished(self):
        if self._pending_expanded:
            # Remove the cap once fully open so the content can still grow
            # or shrink naturally afterward (e.g. a table gaining rows)
            # instead of staying stuck at whatever height it animated to.
            self.content_widget.setMaximumHeight(16777215)
        else:
            self.content_widget.setVisible(False)
        self.updateGeometry()
        parent_widget = self.parentWidget()
        if parent_widget is not None and parent_widget.layout() is not None:
            parent_widget.layout().activate()

        # A fresh QGraphicsDropShadowEffect, not reusing the detached one
        # -- Qt's ownership semantics around setGraphicsEffect(None) don't
        # clearly guarantee the old effect object survives, so recreating
        # it (the same thing Card.__init__ does) is the safe option
        # rather than risking a "wrapped C/C++ object has been deleted"
        # error on a stale reference.
        for card in getattr(self, "_suspended_cards", []):
            card._shadow = QGraphicsDropShadowEffect(card)
            card.setGraphicsEffect(card._shadow)
            card._apply_shadow(manager.colors)
        self._suspended_cards = []

    def _update_button_text(self):
        arrow = "▾" if self.toggle_button.isChecked() else "▸"
        self.toggle_button.setText(f"{arrow} {self._title}")


class HoverRevealPanel(QWidget):
    """Wraps a content widget (a big countdown timer, say) that stays
    tucked away behind a small always-visible "peek" hint until the mouse
    hovers over this panel, then slides down to reveal the full content --
    for a big, otherwise-distracting readout that should recede when
    you're not actually checking it, discoverable via the hint rather than
    disappearing without a trace. Same animated-maximumHeight mechanic as
    CollapsibleSection above (hover-driven instead of a toggle button),
    plus a short linger delay after the mouse leaves so briefly crossing
    over it while moving the mouse elsewhere doesn't cause a flicker."""

    HIDE_DELAY_MS = 400
    # A down-chevron, not text -- same "points down = reveals more below"
    # visual language CollapsibleSection's own expand arrow already uses.
    DEFAULT_PEEK_TEXT = "▾"

    def __init__(self, content_widget, peek_text=DEFAULT_PEEK_TEXT, parent=None):
        super().__init__(parent)
        self.content_widget = content_widget
        self.setMouseTracking(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.peek_label = QLabel(peek_text)
        self.peek_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.peek_label)
        layout.addWidget(content_widget)

        self._expanded = False
        content_widget.setVisible(False)
        content_widget.setMaximumHeight(0)

        self._animation = QPropertyAnimation(content_widget, b"maximumHeight", self)
        self._animation.setDuration(COLLAPSE_ANIMATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.finished.connect(self._on_animation_finished)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._collapse)

        self._apply_colors(manager.colors)
        manager.changed.connect(self._on_theme_changed)

    def enterEvent(self, event):
        self._hide_timer.stop()
        self._expand()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hide_timer.start(self.HIDE_DELAY_MS)
        super().leaveEvent(event)

    def _expand(self):
        if self._expanded:
            return
        self._expanded = True
        self.peek_label.setVisible(False)
        self._animate_to(self.content_widget.sizeHint().height())

    def _collapse(self):
        if not self._expanded:
            return
        self._expanded = False
        self.peek_label.setVisible(True)
        self._animate_to(0)

    def _animate_to(self, target_height):
        self.content_widget.setVisible(True)
        start_height = self.content_widget.height()

        # Same fix as CollapsibleSection above, same reason: a Card's
        # QGraphicsDropShadowEffect can lose sync with rapid animated
        # resizing (confirmed there via real captured frames). No
        # current caller wraps a Card in this, but this is a
        # general-purpose reveal component -- cheap to guard now rather
        # than rediscover the same bug the next time one does.
        self._suspended_cards = self.content_widget.findChildren(Card)
        for card in self._suspended_cards:
            card.setGraphicsEffect(None)

        self._animation.stop()
        self._animation.setStartValue(start_height)
        self._animation.setEndValue(target_height)
        self._animation.start()

    def _on_animation_finished(self):
        if self._expanded:
            self.content_widget.setMaximumHeight(16777215)
        else:
            self.content_widget.setVisible(False)
        self.updateGeometry()
        parent_widget = self.parentWidget()
        if parent_widget is not None and parent_widget.layout() is not None:
            parent_widget.layout().activate()

        for card in getattr(self, "_suspended_cards", []):
            card._shadow = QGraphicsDropShadowEffect(card)
            card.setGraphicsEffect(card._shadow)
            card._apply_shadow(manager.colors)
        self._suspended_cards = []

    def _on_theme_changed(self, theme_id, colors):
        self._apply_colors(colors)

    def _apply_colors(self, colors):
        self.peek_label.setStyleSheet(
            f"color: {colors['text_dim']}; font-size: 18px; font-weight: bold; padding: 2px;"
        )
