# A small always-on "attention span" video player embedded in the corner
# of Monitor Lockdown -- auto-plays the configured channel's most recent
# NOT-YET-WATCHED upload (see services/youtube_feed.py), and automatically
# advances to the next one once it ends, never repeating something
# already watched.
#
# User-confirmed to run DURING lockdown itself, not just the normal app
# window -- a deliberate, informed exception to this app's usual anti-
# distraction design (webcam-monitored focus, time-boxed breaks,
# Discord/Spotify pushed to a second monitor during study), made
# knowingly rather than by default.

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtWebEngineCore import QWebEnginePage
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel

import youtube_feed

PLAYER_WIDTH = 480
PLAYER_HEIGHT = 270
DEFAULT_CHANNEL_HANDLE = "@Zerggyyyy"

# The page's own origin -- used as BOTH the player's `origin` playerVar
# AND setHtml()'s baseUrl (must match; see _load_next()). Deliberately
# NOT https://www.youtube.com itself: that was the actual bug behind the
# "player initializes, then immediately errors" failure -- a page
# claiming to genuinely BE youtube.com, asking YouTube's own player to
# embed inside itself, gets rejected (observed real error code 152, not
# one of the standard documented ones, right after player_ready). A
# neutral, YouTube-unaffiliated origin is what this parameter is
# actually meant for -- doesn't need to resolve to a real server, since
# the content itself is injected via setHtml(), never actually fetched
# from this URL.
_PAGE_ORIGIN = "https://studywarden.local"

# document.title signals from the injected page, caught by QWebEnginePage.titleChanged --
# a plain-JS way to notify Python without needing full QWebChannel setup. One prefix, a
# colon-separated event name, and an optional payload -- covers every milestone from "the
# API script loaded" through playback state changes, not just the two outcomes that matter
# functionally (ended/error), specifically so a stalled pipeline shows exactly where it
# stalled instead of just "nothing happened".
_SIGNAL_PREFIX = "__SW_VIDEO__:"
_HTML_LOADED_TITLE = _SIGNAL_PREFIX + "html_loaded"       # the page's own JS started running at all
_API_READY_TITLE = _SIGNAL_PREFIX + "api_ready"            # the external iframe_api script loaded + called back
_PLAYER_READY_TITLE = _SIGNAL_PREFIX + "player_ready"      # YT.Player() actually initialized
_STATE_CHANGE_PREFIX = _SIGNAL_PREFIX + "state:"           # + a YT.PlayerState number, on EVERY change
_VIDEO_ENDED_TITLE = _SIGNAL_PREFIX + "ended"
_VIDEO_ERROR_PREFIX = _SIGNAL_PREFIX + "error:"            # + the YT IFrame API error code
# Whichever of these is the LAST one seen for a given video tells you exactly where the
# pipeline stalled -- see _on_title_changed()'s diagnostic printing.

# YouTube IFrame Player API error codes -- see
# https://developers.google.com/youtube/iframe_api_reference#onError
_YT_ERROR_MESSAGES = {
    "2": "invalid video ID",
    "5": "this video can't be played in the HTML5 player",
    "100": "video not found (removed or private)",
    "101": "the video's owner has disabled embedding",
    "150": "the video's owner has disabled embedding",
}
_YT_STATE_NAMES = {
    "-1": "unstarted", "0": "ended", "1": "playing", "2": "paused", "3": "buffering", "5": "cued",
}

_PLAYER_HTML = """<!DOCTYPE html>
<html><head><style>body{{margin:0;background:#000;overflow:hidden;}}</style>
<script>document.title = '{html_loaded_title}';</script>
</head>
<body>
<div id="player"></div>
<script src="https://www.youtube.com/iframe_api"></script>
<script>
var player;
function onYouTubeIframeAPIReady() {{
    document.title = '{api_ready_title}';
    player = new YT.Player('player', {{
        height: '{height}', width: '{width}', videoId: '{video_id}',
        playerVars: {{autoplay: 1, mute: 1, controls: 1, origin: '{page_origin}'}},
        events: {{'onReady': onPlayerReady, 'onStateChange': onPlayerStateChange, 'onError': onPlayerError}}
    }});
}}
function onPlayerReady(event) {{
    document.title = '{player_ready_title}';
}}
function onPlayerStateChange(event) {{
    if (event.data === YT.PlayerState.ENDED) {{
        document.title = '{ended_title}';
    }} else {{
        document.title = '{state_prefix}' + event.data;
    }}
}}
function onPlayerError(event) {{
    document.title = '{error_prefix}' + event.data;
}}
</script>
</body></html>"""


class _DiagnosticWebEnginePage(QWebEnginePage):
    """Forwards the embedded page's own JS console output (errors,
    warnings, network/script-load failures) to stdout -- without this,
    a failure INSIDE the embedded page (the IFrame API script failing to
    load, a JS exception, etc.) is completely invisible; nothing in Qt
    itself surfaces it. Same "print raw diagnostics so a real failure
    can actually be seen" approach used for the LLM estimate debugging
    earlier -- run the app from a terminal to see this output."""

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        print(f"[AttentionVideoPlayer JS console] {message} (line {line_number})")


class AttentionVideoPlayer(QWidget):
    """A small (PLAYER_WIDTH x PLAYER_HEIGHT) always-on corner video
    player. Best-effort throughout: any failure (can't resolve the
    channel, can't reach the feed, everything's already been watched)
    just shows a small status message in place of the player, never
    blocks or breaks the lockdown session around it."""

    def __init__(self, channel_handle=DEFAULT_CHANNEL_HANDLE, parent=None):
        super().__init__(parent)
        self.channel_handle = channel_handle
        self.setFixedSize(PLAYER_WIDTH, PLAYER_HEIGHT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.status_label = QLabel("Loading video...")
        self.status_label.setFixedSize(PLAYER_WIDTH, PLAYER_HEIGHT)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("background-color: #000; color: #AAAAAA; font-size: 11px; padding: 6px;")
        layout.addWidget(self.status_label)

        self.view = QWebEngineView()
        self.view.setFixedSize(PLAYER_WIDTH, PLAYER_HEIGHT)
        self.view.setPage(_DiagnosticWebEnginePage(self.view))  # forwards JS console output to stdout
        # App-controlled embedded content, not a random page someone else
        # authored -- autoplay is a deliberate choice here, not something
        # to leave blocked by Chromium's default no-user-gesture autoplay
        # policy. Starts muted (see playerVars in _PLAYER_HTML) -- you can
        # unmute from the player's own controls if you want sound.
        self.view.settings().setAttribute(self.view.settings().WebAttribute.PlaybackRequiresUserGesture, False)
        self.view.page().titleChanged.connect(self._on_title_changed)
        self.view.hide()
        layout.addWidget(self.view)

        self._channel_id = None
        self._videos = []
        self._showing_recommended_fallback = False  # see _load_next()
        self._load_next()

    def _show_status(self, text):
        self.status_label.setText(text)
        self.status_label.show()
        self.view.hide()

    def _load_next(self):
        """Resolves the channel (once, cached) if needed, then loads the
        newest not-yet-watched video -- re-fetching the feed if the
        cached list has been exhausted (nothing new posted since), in
        case something new has gone up.

        If EVERY video from this channel has already been watched (even
        after a fresh re-fetch), falls back to "recommended options"
        rather than going dark: replays the channel's newest upload
        anyway. _PLAYER_HTML never restricts `rel`, so YouTube's OWN
        embedded player already shows its native related-videos overlay
        once a video ends -- reusing that gets real, YouTube-curated
        recommendations the user can click into, with no separate
        recommendations API/key needed. Once in this fallback mode,
        _on_title_changed() stops auto-advancing on ENDED (see there)
        so that native overlay stays up instead of being immediately
        replaced by another forced replay."""
        try:
            if self._channel_id is None:
                self._channel_id = youtube_feed.resolve_channel_id(self.channel_handle)
            if not self._videos:
                self._videos = youtube_feed.fetch_recent_video_ids(self._channel_id)
            picked = youtube_feed.pick_next_unwatched(self._channel_id, self._videos)
            if picked is None:
                # exhausted the cached list -- re-fetch once in case something new posted
                self._videos = youtube_feed.fetch_recent_video_ids(self._channel_id)
                picked = youtube_feed.pick_next_unwatched(self._channel_id, self._videos)

            self._showing_recommended_fallback = picked is None and bool(self._videos)
            if self._showing_recommended_fallback:
                picked = self._videos[0]
        except youtube_feed.YoutubeFeedError as e:
            self._show_status(f"Video unavailable: {e}")
            return

        if picked is None:
            self._show_status("No videos available right now.")
            return

        video_id, title_text = picked
        if self._showing_recommended_fallback:
            print(f"[AttentionVideoPlayer] no unwatched videos left -- replaying newest upload "
                  f"{video_id} ({title_text!r}) so YouTube's own suggested-videos overlay takes over once it ends")
        else:
            print(f"[AttentionVideoPlayer] loading video {video_id} ({title_text!r})")
        youtube_feed.mark_watched(self._channel_id, video_id)
        html = _PLAYER_HTML.format(
            height=PLAYER_HEIGHT, width=PLAYER_WIDTH, video_id=video_id, page_origin=_PAGE_ORIGIN,
            html_loaded_title=_HTML_LOADED_TITLE, api_ready_title=_API_READY_TITLE,
            player_ready_title=_PLAYER_READY_TITLE, state_prefix=_STATE_CHANGE_PREFIX,
            ended_title=_VIDEO_ENDED_TITLE, error_prefix=_VIDEO_ERROR_PREFIX,
        )
        self.view.setHtml(html, baseUrl=QUrl(_PAGE_ORIGIN))
        self.status_label.hide()
        self.view.show()

    def _on_title_changed(self, title):
        if not title.startswith(_SIGNAL_PREFIX):
            return  # some other, irrelevant title change (e.g. YouTube's own internal navigation)

        if title == _HTML_LOADED_TITLE:
            print("[AttentionVideoPlayer] page HTML loaded, JS running")
        elif title == _API_READY_TITLE:
            print("[AttentionVideoPlayer] YouTube IFrame API script loaded")
        elif title == _PLAYER_READY_TITLE:
            print("[AttentionVideoPlayer] player initialized and ready")
        elif title.startswith(_STATE_CHANGE_PREFIX):
            code = title[len(_STATE_CHANGE_PREFIX):]
            print(f"[AttentionVideoPlayer] playback state -> {_YT_STATE_NAMES.get(code, code)}")
        elif title == _VIDEO_ENDED_TITLE:
            if self._showing_recommended_fallback:
                print("[AttentionVideoPlayer] recommended-fallback video ended -- leaving YouTube's own "
                      "suggested-videos overlay up instead of forcing another replay")
            else:
                print("[AttentionVideoPlayer] video ended, loading next")
                self._load_next()
        elif title.startswith(_VIDEO_ERROR_PREFIX):
            code = title[len(_VIDEO_ERROR_PREFIX):]
            reason = _YT_ERROR_MESSAGES.get(code, f"unrecognized error code {code}")
            print(f"[AttentionVideoPlayer] YouTube player error {code} ({reason}) -- skipping to the next video")
            self._show_status(f"Couldn't play that video ({reason}) -- trying the next one...")
            self._load_next()
