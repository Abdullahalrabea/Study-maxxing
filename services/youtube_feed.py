# Resolves a YouTube channel handle (e.g. "@Zerggyyyy") to its real
# channel ID and fetches its most recent uploads -- both via plain,
# public HTTP requests, no API key needed. Used by ui/attention_video.py's
# small corner player during Monitor Lockdown (see lockdown_window.py).
#
# Channel ID resolution reads the canonical link tag straight out of the
# channel page's own server-rendered HTML (the same metadata a real
# browser sees on page load, before any client-side JS runs) -- a plain
# GET request, not scraping rendered/dynamic content. Recent uploads come
# from the channel's own public RSS feed
# (youtube.com/feeds/videos.xml?channel_id=...), which YouTube provides
# specifically for this purpose.

import re
import xml.etree.ElementTree as ET

import requests
from PyQt6.QtCore import QSettings

SETTINGS_ORG = "StudyWarden"
SETTINGS_APP = "StudyWarden"
SETTINGS_KEY_CHANNEL_ID_PREFIX = "attention_video/channel_id/"  # + handle
SETTINGS_KEY_WATCHED_PREFIX = "attention_video/watched/"        # + channel_id

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
REQUEST_TIMEOUT_SEC = 15

_CHANNEL_ID_RE = re.compile(r'canonical" href="https://www\.youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})"')
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


class YoutubeFeedError(Exception):
    pass


def resolve_channel_id(handle):
    """handle: e.g. "@Zerggyyyy" (the leading @ is added if missing).
    Returns the channel's real ID (UC...). Cached in QSettings per-handle
    so this only ever costs one real request per handle, not one per
    lookup -- a channel's ID never changes once created."""
    handle = handle if handle.startswith("@") else f"@{handle}"
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    cache_key = SETTINGS_KEY_CHANNEL_ID_PREFIX + handle
    cached = settings.value(cache_key)
    if cached:
        return cached

    try:
        resp = requests.get(
            f"https://www.youtube.com/{handle}", headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT_SEC
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise YoutubeFeedError(f"Couldn't reach YouTube to resolve {handle}: {e}") from e

    match = _CHANNEL_ID_RE.search(resp.text)
    if not match:
        raise YoutubeFeedError(f"Couldn't find a channel ID on {handle}'s page -- has YouTube changed its page format?")
    channel_id = match.group(1)
    settings.setValue(cache_key, channel_id)
    return channel_id


def fetch_recent_video_ids(channel_id):
    """Returns [(video_id, title), ...] for the channel's most recent
    uploads (typically ~15), NEWEST FIRST -- straight from the channel's
    own public RSS feed."""
    try:
        resp = requests.get(
            f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}", timeout=REQUEST_TIMEOUT_SEC
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise YoutubeFeedError(f"Couldn't fetch {channel_id}'s upload feed: {e}") from e

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        raise YoutubeFeedError(f"Couldn't parse {channel_id}'s upload feed: {e}") from e

    videos = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        video_id_el = entry.find("yt:videoId", _ATOM_NS)
        title_el = entry.find("atom:title", _ATOM_NS)
        if video_id_el is not None and video_id_el.text:
            videos.append((video_id_el.text, title_el.text if title_el is not None else ""))
    return videos


def _watched_key(channel_id):
    return SETTINGS_KEY_WATCHED_PREFIX + channel_id


def load_watched(channel_id):
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    raw = settings.value(_watched_key(channel_id)) or []
    # QSettings hands back a bare str instead of a list if only one value was ever stored under this key
    if isinstance(raw, str):
        return {raw}
    return set(raw)


def mark_watched(channel_id, video_id):
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    watched = load_watched(channel_id)
    watched.add(video_id)
    settings.setValue(_watched_key(channel_id), list(watched))


def pick_next_unwatched(channel_id, videos):
    """videos: [(video_id, title), ...], newest first (see
    fetch_recent_video_ids()). Returns (video_id, title) for the newest
    video NOT already in this channel's watched history, or None if
    every video in the given list has already been watched (e.g.
    nothing new posted since the last check)."""
    watched = load_watched(channel_id)
    for video_id, title in videos:
        if video_id not in watched:
            return video_id, title
    return None
