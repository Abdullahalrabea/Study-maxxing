# Freehand annotation tools for Monitor Lockdown: a highlighter + pen you
# can draw with directly on top of the study material, and a separate
# blank scratchpad panel for notes/sketches that aren't tied to any
# specific page. Both share DrawingCanvas -- the only difference is
# whether it has real content behind it (the PDF annotation overlay,
# transparent) or paints its own flat page (the scratchpad).
#
# The PDF is rendered as flat page IMAGES (see lockdown_window.py --
# PyMuPDF rasterizes each page, there's no selectable text layer), so
# "highlighting text" here means a translucent freehand marker you drag
# across whatever you want to mark, the same way you'd use a real
# highlighter over a printed page -- not text-aware selection.
#
# Strokes are stored as FRACTIONS of the canvas's current size
# (0.0-1.0), not raw pixel coordinates -- so a stroke stays correctly
# positioned when the canvas resizes (e.g. the PDF re-rendering at a new
# zoom level) instead of needing separate rescaling logic or drifting.

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPainter, QPainterPath, QPen, QColor

import theme
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton

PEN_WIDTH = 3
HIGHLIGHTER_WIDTH = 18
HIGHLIGHTER_ALPHA = 90  # out of 255 -- translucent, like a real highlighter marking over text/content

PEN_COLORS = ["#1A1A1A", "#D62828", "#1D4ED8", "#2A9D3F"]           # black, red, blue, green
HIGHLIGHTER_COLORS = ["#FFEB3B", "#7CFC9A", "#8FD3FF", "#FF8FD8"]   # yellow, mint, sky, pink -- classic marker shades


class Stroke:
    """One continuous pen/highlighter drag -- points are (x_frac, y_frac)
    fractions of the canvas's size AT DRAW TIME, so painting always
    rescales them to whatever the canvas's CURRENT size is."""
    __slots__ = ("tool", "color", "width", "points")

    def __init__(self, tool, color, width):
        self.tool = tool
        self.color = color
        self.width = width
        self.points = []


class DrawingCanvas(QWidget):
    """A freehand drawing surface -- transparent (an annotation layer on
    top of other content) when background_color is None, or opaque (the
    scratchpad's own blank page) when given a hex color. Only actually
    captures mouse input while set_tool() has an active tool ("pen" or
    "highlighter"); with no tool active, clicks/drags pass straight
    through to whatever's underneath, so it doesn't block normal
    scrolling/interaction except while you're deliberately drawing."""

    stroke_added = pyqtSignal()

    def __init__(self, background_color=None, parent=None):
        super().__init__(parent)
        self._background_color = background_color
        self.strokes = []
        self._current_stroke = None
        self._tool = None
        self._color = QColor(PEN_COLORS[0])
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        if background_color is None:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def set_tool(self, tool):
        """tool: "pen", "highlighter", or None (drawing off -- events
        pass through to whatever's behind this canvas again)."""
        self._tool = tool
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, tool is None)

    def set_color(self, hex_color):
        self._color = QColor(hex_color)

    def clear(self):
        self.strokes = []
        self.update()

    def undo(self):
        if self.strokes:
            self.strokes.pop()
            self.update()

    def _width_for_tool(self, tool):
        return HIGHLIGHTER_WIDTH if tool == "highlighter" else PEN_WIDTH

    def _color_for_stroke(self, tool):
        color = QColor(self._color)
        if tool == "highlighter":
            color.setAlpha(HIGHLIGHTER_ALPHA)
        return color

    def mousePressEvent(self, event):
        if self._tool is None or event.button() != Qt.MouseButton.LeftButton:
            return
        self._current_stroke = Stroke(self._tool, self._color_for_stroke(self._tool), self._width_for_tool(self._tool))
        self._add_point(event.position())

    def mouseMoveEvent(self, event):
        if self._current_stroke is None:
            return
        self._add_point(event.position())

    def mouseReleaseEvent(self, event):
        if self._current_stroke is None:
            return
        if len(self._current_stroke.points) > 1:
            self.strokes.append(self._current_stroke)
            self.stroke_added.emit()
        self._current_stroke = None
        self.update()

    def _add_point(self, pos):
        w, h = max(1, self.width()), max(1, self.height())
        self._current_stroke.points.append((pos.x() / w, pos.y() / h))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._background_color is not None:
            painter.fillRect(self.rect(), QColor(self._background_color))
        w, h = max(1, self.width()), max(1, self.height())
        for stroke in self.strokes:
            self._paint_stroke(painter, stroke, w, h)
        if self._current_stroke is not None:
            self._paint_stroke(painter, self._current_stroke, w, h)
        painter.end()

    def _paint_stroke(self, painter, stroke, w, h):
        if len(stroke.points) < 2:
            return
        path = QPainterPath()
        path.moveTo(stroke.points[0][0] * w, stroke.points[0][1] * h)
        for x_frac, y_frac in stroke.points[1:]:
            path.lineTo(x_frac * w, y_frac * h)
        pen = QPen(stroke.color, stroke.width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(path)


class AnnotationToolbar(QWidget):
    """Compact tool-select row for a DrawingCanvas: Pen/Highlighter toggle
    buttons (mutually exclusive -- picking one deactivates the other;
    un-clicking the active one turns drawing off so normal scrolling
    works again), a color swatch row for whichever tool is active, and
    Undo/Clear."""

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.pen_button = QPushButton("Pen")
        self.pen_button.setCheckable(True)
        self.pen_button.clicked.connect(lambda: self._on_tool_clicked("pen"))
        layout.addWidget(self.pen_button)

        self.highlighter_button = QPushButton("Highlighter")
        self.highlighter_button.setCheckable(True)
        self.highlighter_button.clicked.connect(lambda: self._on_tool_clicked("highlighter"))
        layout.addWidget(self.highlighter_button)

        self.color_row = QHBoxLayout()
        layout.addLayout(self.color_row)
        self._color_buttons = []

        undo_button = QPushButton("Undo")
        undo_button.clicked.connect(self.canvas.undo)
        layout.addWidget(undo_button)

        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self.canvas.clear)
        layout.addWidget(clear_button)

        layout.addStretch()
        self._set_colors(PEN_COLORS)

    def _on_tool_clicked(self, tool):
        this_button = self.pen_button if tool == "pen" else self.highlighter_button
        other_button = self.highlighter_button if tool == "pen" else self.pen_button
        if this_button.isChecked():
            other_button.setChecked(False)
            self.canvas.set_tool(tool)
            self._set_colors(HIGHLIGHTER_COLORS if tool == "highlighter" else PEN_COLORS)
        else:
            self.canvas.set_tool(None)

    def deactivate(self):
        """Turns drawing off and un-checks both tool buttons -- e.g. when
        the scratchpad closes, so reopening it doesn't silently start
        capturing mouse events again before you've picked a tool."""
        self.pen_button.setChecked(False)
        self.highlighter_button.setChecked(False)
        self.canvas.set_tool(None)

    def _set_colors(self, colors):
        for btn in self._color_buttons:
            btn.deleteLater()
        self._color_buttons = []
        for hex_color in colors:
            btn = QPushButton()
            btn.setFixedSize(20, 20)
            btn.setStyleSheet(f"background-color: {hex_color}; border: 1px solid #333333; border-radius: {theme.RADIUS_TAG}px;")
            btn.clicked.connect(lambda _checked, c=hex_color: self.canvas.set_color(c))
            self.color_row.addWidget(btn)
            self._color_buttons.append(btn)
        if colors:
            self.canvas.set_color(colors[0])


class ScratchpadPanel(QWidget):
    """A blank page you can draw/write on, independent of whatever
    material is showing.

    Two usage modes, picked via closable:
    - closable=True (the default): floats above everything else in the
      parent window (raise_() on show) rather than replacing the view,
      starts hidden, and open()/its own Close button toggle it -- the
      original pull-up-panel behavior.
    - closable=False: a permanently-docked panel meant to be added
      directly to a real layout (e.g. LockdownWindow's Notepad sidebar
      section) -- no Close button (there'd be no way to reopen it
      without a toggle), starts visible, and does NOT auto-select the
      pen tool the way open() does, since an always-visible canvas
      sitting under your mouse while you scroll/read shouldn't start
      capturing drag strokes until you deliberately pick a tool."""

    closed = pyqtSignal()  # emitted whenever this hides, however that happens -- lets the caller's own
                            # toggle button (e.g. LockdownWindow's) stay in sync without polling

    def __init__(self, parent=None, closable=True, title="Scratchpad"):
        super().__init__(parent)
        self._closable = closable
        self.setAutoFillBackground(True)
        # setAutoFillBackground() is a separate, palette-based mechanism
        # from QSS -- it's why the white background always painted, but
        # WA_StyledBackground is what's actually needed for the QSS
        # border/border-radius below to paint at all (same gotcha
        # Card's own docstring documents, in theme.py).
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Scoped to an ID selector (#ScratchpadPanel), not a bare
        # declaration -- a bare `setStyleSheet("background-color: ...")`
        # on this widget was blocking the app's global themed QPushButton
        # rule from reaching the toolbar's buttons underneath it, since
        # ANY local stylesheet on an ancestor makes Qt stop cascading the
        # app-level stylesheet through for that subtree. Confirmed via a
        # real render: the Pen button came out plain #FFFFFF (invisible
        # against this panel's own white background, no border, no themed
        # color at all) instead of the theme's actual button color.
        # Scoping this rule to an ID selector fixes that -- it only ever
        # matches THIS widget, so descendants go back to resolving their
        # own styling from the app-level stylesheet normally.
        #
        # Background stays hardcoded white deliberately -- this is a
        # drawing/writing surface (the canvas below shares the same
        # #FFFFFF, see DrawingCanvas(background_color="#FFFFFF") below),
        # not a themed panel; ink and highlighter marks need a
        # consistent, legible page regardless of which app theme is
        # active, the same reason a real paper notepad doesn't change
        # color with the room's lighting. Only the border is themed (so
        # it doesn't clash with the active accent) and rounded to match
        # the rest of the Arc-style redesign.
        self.setObjectName("ScratchpadPanel")
        self._apply_border(theme.manager.colors)
        theme.manager.changed.connect(self._on_theme_changed)

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setStyleSheet("font-weight: bold; font-size: 14px; border: none;")
        header.addWidget(title_label)
        header.addStretch()
        if closable:
            close_button = QPushButton("Close")
            close_button.clicked.connect(self.hide)
            header.addWidget(close_button)
        layout.addLayout(header)

        self.canvas = DrawingCanvas(background_color="#FFFFFF")
        self.toolbar = AnnotationToolbar(self.canvas)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas, 1)

        if closable:
            self.hide()

    def _apply_border(self, colors):
        self.setStyleSheet(
            f"#ScratchpadPanel {{ background-color: #FFFFFF; "
            f"border: 2px solid {colors['border']}; border-radius: {theme.RADIUS_CARD}px; }}"
        )

    def _on_theme_changed(self, theme_id, colors):
        self._apply_border(colors)

    def open(self):
        self.show()
        self.raise_()
        self.toolbar.pen_button.setChecked(True)
        self.canvas.set_tool("pen")

    def hideEvent(self, event):
        self.toolbar.deactivate()
        self.closed.emit()
        super().hideEvent(event)
