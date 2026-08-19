# Compact "now playing" + controls widget for Monitor Lockdown:
# Spotify's current track + cover art (read via SMTC, see
# services/spotify_now_playing.py) plus previous/play-pause/next
# transport buttons (also SMTC -- the same mechanism a keyboard's
# hardware media keys use) and a volume slider (Windows Core Audio
# per-app volume, see services/spotify_volume.py -- a completely
# separate mechanism from SMTC, which has no concept of volume at all).
# No Spotify Developer app/API key/OAuth anywhere in this. Polls
# periodically to catch track/volume changes made from elsewhere (e.g.
# Spotify's own window, or a hardware media key); only re-decodes the
# cover art when the track actually changed, and never overwrites the
# volume slider while you're actively dragging it.

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSlider

import theme
import spotify_now_playing
import spotify_volume

ART_SIZE = 90
POLL_MS = 5000
CONTROL_WIDTH = ART_SIZE + 20


class SpotifyNowPlayingWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = None
        self._volume_worker = None
        self._last_track_key = None

        # Pinned to its natural content width -- without this, header_row
        # (a QHBoxLayout with a trailing addStretch()) was allocating this
        # widget far more width than it needs (measured ~354px vs ~110px
        # of actual content once pose_label collapses to zero). Since
        # art_label/transport_row are explicitly AlignHCenter'd within
        # that extra space but title_label/artist_label/volume_slider
        # weren't, the two groups centered differently within the same
        # oversized box -- looking like two separate side-by-side columns
        # instead of one stack, which is exactly the bug this caused.
        self.setFixedWidth(CONTROL_WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.art_label = QLabel()
        self.art_label.setFixedSize(ART_SIZE, ART_SIZE)
        self.art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.art_label.setStyleSheet("background-color: #000000;")
        layout.addWidget(self.art_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.title_label = QLabel("")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setWordWrap(True)
        self.title_label.setFixedWidth(CONTROL_WIDTH)
        self.title_label.setStyleSheet("font-size: 10px; font-weight: bold;")
        layout.addWidget(self.title_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.artist_label = QLabel("")
        self.artist_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.artist_label.setWordWrap(True)
        self.artist_label.setFixedWidth(CONTROL_WIDTH)
        layout.addWidget(self.artist_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Transport: previous / play-pause / next -- all via the same
        # SMTC session used for reading now-playing info. Plain ASCII
        # glyphs, not the U+23xx "media control symbols" block (⏮ ⏭ ⏸
        # etc.) -- confirmed via a real screenshot from an actual run
        # that those render as empty boxes on this machine's font setup
        # (▶, an OLDER/different block, also failed -- so this sticks to
        # plain ASCII specifically, not just "an older Unicode block").
        #
        # Sized+styled with their OWN stylesheet, not the app's global
        # QPushButton rule (padding: 6px 14px) -- that padding alone is
        # wider than these buttons' whole 30px, which was clipping the
        # glyphs down to invisible and made all three look like empty
        # boxes (confirmed by reading theme.py's global rule directly,
        # the same class of bug the homepage settings gear button
        # (theme.py's HeaderBanner) already had to work around the same
        # way -- see its own setFixedSize+dedicated-stylesheet comment).
        transport_row = QHBoxLayout()
        transport_row.setContentsMargins(0, 4, 0, 4)
        transport_row.setSpacing(3)
        self.prev_button = QPushButton("<<")
        self.prev_button.setFixedSize(30, 26)
        self.prev_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.prev_button.clicked.connect(lambda: self._send_command(spotify_now_playing.PREVIOUS))
        transport_row.addWidget(self.prev_button)

        self.play_pause_button = QPushButton(">")
        self.play_pause_button.setFixedSize(30, 26)
        self.play_pause_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_pause_button.clicked.connect(lambda: self._send_command(spotify_now_playing.TOGGLE_PLAY_PAUSE))
        transport_row.addWidget(self.play_pause_button)

        self.next_button = QPushButton(">>")
        self.next_button.setFixedSize(30, 26)
        self.next_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.next_button.clicked.connect(lambda: self._send_command(spotify_now_playing.NEXT))
        transport_row.addWidget(self.next_button)
        layout.addLayout(transport_row)
        layout.setAlignment(transport_row, Qt.AlignmentFlag.AlignHCenter)

        # Volume: Spotify's own per-app volume (Windows Core Audio), NOT
        # the system master volume. Only the actual release sends a
        # set_volume() call (each one does a real ~1s scan across every
        # audio device -- see spotify_volume.py) -- dragging itself is
        # instant, free, purely local slider feedback.
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setFixedWidth(CONTROL_WIDTH)
        self.volume_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.volume_slider.sliderReleased.connect(self._on_volume_released)
        layout.addWidget(self.volume_slider, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._apply_theme(theme.manager.colors)
        theme.manager.changed.connect(self._on_theme_changed)

        self._show_idle()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(POLL_MS)
        self._refresh()

    def _apply_theme(self, colors):
        self.title_label.setStyleSheet(
            f"font-size: 10px; font-weight: bold; color: {colors['text']};"
        )
        self.artist_label.setStyleSheet(
            f"font-size: 9px; color: {colors['text_dim']};"
        )
        button_style = f"""
            QPushButton {{
                background-color: {colors['surface']};
                border: 1px solid {colors['border']};
                border-radius: {theme.RADIUS_TAG}px;
                color: {colors['text']};
                padding: 0px;
                font-size: 11px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {colors['surface_hover']};
                border-color: {colors['accent']};
            }}
            QPushButton:pressed {{
                background-color: {colors['selection_bg']};
            }}
            QPushButton:disabled {{
                color: {colors['text_dim']};
                border-color: {colors['text_dim']};
            }}
        """
        self.prev_button.setStyleSheet(button_style)
        self.play_pause_button.setStyleSheet(button_style)
        self.next_button.setStyleSheet(button_style)
        self.volume_slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                height: 4px;
                background: {colors['border']};
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: {colors['accent']};
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: {colors['accent']};
                width: 12px;
                margin: -5px 0;
                border-radius: 6px;
            }}
            QSlider::handle:horizontal:disabled {{
                background: {colors['text_dim']};
            }}
        """)

    def _on_theme_changed(self, theme_id, colors):
        self._apply_theme(colors)

    def _set_controls_enabled(self, enabled):
        self.prev_button.setEnabled(enabled)
        self.play_pause_button.setEnabled(enabled)
        self.next_button.setEnabled(enabled)
        self.volume_slider.setEnabled(enabled)

    def _show_idle(self):
        self.art_label.setPixmap(QPixmap())
        self.title_label.setText("")
        self.artist_label.setText("Spotify: not playing")
        self._last_track_key = None
        self._set_controls_enabled(False)

    def _refresh(self):
        if self._worker is None or not self._worker.isRunning():
            self._worker = spotify_now_playing.NowPlayingWorker(self)
            self._worker.done.connect(self._on_now_playing)
            self._worker.start()

        # Never fight the user for the slider handle mid-drag.
        if not self.volume_slider.isSliderDown():
            if self._volume_worker is None or not self._volume_worker.isRunning():
                self._volume_worker = spotify_volume.VolumeReadWorker(self)
                self._volume_worker.done.connect(self._on_volume_read)
                self._volume_worker.start()

    def _on_now_playing(self, info):
        self._worker = None
        if info is None:
            self._show_idle()
            return

        self._set_controls_enabled(True)
        title = info["title"] or "(unknown title)"
        artist = info["artist"] or ""
        self.play_pause_button.setText("||" if info["is_playing"] else ">")
        status_glyph = "Playing:" if info["is_playing"] else "Paused:"
        self.title_label.setText(f"{status_glyph} {title}")
        self.artist_label.setText(artist)

        # Only re-decode/repaint the cover art when the track actually
        # changed -- every poll re-fetches metadata (cheap), but
        # decoding a ~100KB image every 5s for no reason is wasteful.
        track_key = (title, artist, info["album"])
        if track_key != self._last_track_key:
            self._last_track_key = track_key
            cover_bytes = info["cover_bytes"]
            if cover_bytes:
                pixmap = QPixmap()
                pixmap.loadFromData(cover_bytes)
                self.art_label.setPixmap(pixmap.scaled(
                    ART_SIZE, ART_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                ))
            else:
                self.art_label.setPixmap(QPixmap())

    def _on_volume_read(self, level):
        self._volume_worker = None
        if level is not None and not self.volume_slider.isSliderDown():
            self.volume_slider.setValue(round(level * 100))

    def _send_command(self, command):
        worker = spotify_now_playing.TransportCommandWorker(command, self)
        worker.done.connect(self._on_command_done)
        worker.start()

    def _on_command_done(self, command, succeeded):
        if succeeded:
            # A quick extra poll so the icon/track reflect the change
            # right away, rather than waiting up to POLL_MS.
            self._refresh()

    def _on_volume_released(self):
        level = self.volume_slider.value() / 100.0
        worker = spotify_volume.VolumeWriteWorker(level, self)
        worker.start()

    def shutdown(self):
        """Called from LockdownWindow.stop_lockdown() -- stops the
        polling timer AND waits briefly for any in-flight worker thread,
        so nothing keeps refreshing once lockdown ends. The wait matters:
        a QThread destroyed while still running (e.g. mid-way through
        winsdk's WinRT round-trip or pycaw's ~1s device scan) can crash
        the whole process at exit -- confirmed in practice, not just a
        theoretical concern."""
        self._timer.stop()
        if self._worker is not None:
            self._worker.wait(1000)
        if self._volume_worker is not None:
            self._volume_worker.wait(1000)
