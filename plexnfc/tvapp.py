"""Hand movies to the Movie Book Apple TV app (tvos/MovieBook).

The app can't be sent commands directly, but Home Assistant can open it, and
it then asks plexnfc what to do: ``GET /tv/inbox?player=NAME&after=ID`` returns
the newest command for that Apple TV, and ``POST /tv/status`` reports what's
playing so /now and the control tags work.
"""
from __future__ import annotations

import itertools
import threading
import time
import uuid
from urllib.parse import urlencode

# A play command waits this long for the app to open and pick it up; controls
# are only useful straight away.
PLAY_TTL = 120
CONTROL_TTL = 20
STATUS_TTL = 30  # the app reports every few seconds while it's open


class AppInbox:
    def __init__(self):
        self._lock = threading.Lock()
        self._ids = itertools.count(int(time.time()))  # stays ahead of the app's last ID after a restart
        self._commands: dict[str, dict] = {}
        self._status: dict[str, tuple[float, dict]] = {}

    def send(self, player: str, action: str, **fields) -> dict:
        command = {"id": next(self._ids), "action": action, **fields}
        with self._lock:
            self._commands[player.casefold()] = {"sent": time.monotonic(), "command": command}
        return command

    def next(self, player: str, after: int) -> dict | None:
        with self._lock:
            entry = self._commands.get(player.casefold())
        if not entry or entry["command"]["id"] <= after:
            return None
        ttl = PLAY_TTL if entry["command"]["action"] == "play" else CONTROL_TTL
        if time.monotonic() - entry["sent"] > ttl:
            return None
        return entry["command"]

    def set_status(self, player: str, status: dict) -> None:
        with self._lock:
            self._status[player.casefold()] = (time.monotonic(), status)

    def status(self, player: str) -> dict | None:
        with self._lock:
            seen, status = self._status.get(player.casefold(), (0, None))
        return status if status and time.monotonic() - seen < STATUS_TTL else None


def plex_hls_url(plex_url: str, token: str, rating_key, start: int, player: str) -> str:
    """Plex's HLS stream for a movie, which AVPlayer on Apple TV plays as is.

    Plex copies the video when the Apple TV can play it and converts only what
    it can't (MKV containers, some audio), so most movies start without a full
    transcode.
    """
    session = str(uuid.uuid4())
    params = {
        "path": f"/library/metadata/{rating_key}",
        "protocol": "hls", "fastSeek": 1, "directPlay": 0, "directStream": 1, "directStreamAudio": 1,
        "mediaIndex": 0, "partIndex": 0, "subtitles": "none", "offset": int(start),
        "session": session, "X-Plex-Session-Identifier": session,
        "X-Plex-Client-Identifier": f"moviebook-{player}", "X-Plex-Product": "Movie Book",
        "X-Plex-Platform": "iOS", "X-Plex-Device": "Apple TV", "X-Plex-Token": token,
    }
    return f"{plex_url}/video/:/transcode/universal/start.m3u8?{urlencode(params)}"


def jellyfin_hls_url(jellyfin_url: str, api_key: str, item_id: str, start: int, player: str) -> str:
    """Jellyfin's HLS stream for a movie: HEVC/H.264 copied into fMP4 segments where possible."""
    params = {
        "MediaSourceId": item_id, "DeviceId": f"moviebook-{player}", "PlaySessionId": uuid.uuid4().hex,
        "api_key": api_key, "VideoCodec": "hevc,h264", "AudioCodec": "aac,ac3,eac3",
        "SegmentContainer": "mp4", "TranscodingMaxAudioChannels": 6, "MaxStreamingBitrate": 120_000_000,
        "StartTimeTicks": int(start) * 10_000_000,
    }
    return f"{jellyfin_url}/Videos/{item_id}/master.m3u8?{urlencode(params)}"
