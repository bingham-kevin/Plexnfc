"""Hand movies to the Marquee Apple TV app (tvos/MovieBook).

The app can't be sent commands directly, but Home Assistant can open it, and
it then asks plexnfc what to do: ``GET /tv/inbox?after=ID`` returns the newest
command for that Apple TV, and ``POST /tv/status`` reports what's playing so
/now and the control tags work. The app can also browse the library itself.

Every app request carries the secret the Apple TV got when it was paired
(``Authorization: Bearer ...``): stream addresses include the Plex token, so
only Apple TVs you approved get them.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import secrets
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


PAIRING_TTL = 600  # a pairing code is good for 10 minutes


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class AppPairings:
    """Apple TVs allowed to use the app API.

    An Apple TV asks to pair and shows a 6-digit code; you approve the code in
    plexnfc's panel, and the Apple TV then collects a secret it sends with every
    request. Only a hash of each secret is saved, in ``path``.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}
        try:
            with open(path) as f:
                self._devices: dict[str, dict] = json.load(f).get("devices", {})
        except (OSError, ValueError):
            self._devices = {}

    def _save(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump({"devices": self._devices}, f)
        os.replace(tmp, self.path)

    def start(self, name: str) -> dict:
        name = " ".join(name.split())[:40] or "Apple TV"
        pair_id = secrets.token_urlsafe(16)
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self._lock:
            self._pending = {k: v for k, v in self._pending.items() if time.monotonic() - v["created"] < PAIRING_TTL}
            self._pending[pair_id] = {"name": name, "code": code, "created": time.monotonic(), "secret": None}
        return {"pair_id": pair_id, "code": code}

    def pending(self) -> list[dict]:
        with self._lock:
            return [{"pair_id": k, "name": v["name"], "code": v["code"]} for k, v in self._pending.items()
                    if v["secret"] is None and time.monotonic() - v["created"] < PAIRING_TTL]

    def approve(self, pair_id: str, name: str | None = None) -> bool:
        """Pair the Apple TV, under ``name`` if given (tvOS calls every Apple TV "Apple TV")."""
        with self._lock:
            entry = self._pending.get(pair_id)
            if not entry or time.monotonic() - entry["created"] > PAIRING_TTL:
                return False
            entry["name"] = self._unique_name(" ".join((name or "").split())[:40] or entry["name"])
            entry["secret"] = secrets.token_urlsafe(32)
            self._devices[uuid.uuid4().hex] = {"name": entry["name"], "hash": _hash(entry["secret"]),
                                               "paired": int(time.time())}
            self._save()
        return True

    def _unique_name(self, name: str) -> str:
        """``name``, or ``name 2``, ``name 3``… if another Apple TV already has it, since taps find
        an Apple TV by name."""
        taken = {d["name"].lower() for d in self._devices.values()}
        candidate, n = name, 1
        while candidate.lower() in taken:
            n += 1
            candidate = f"{name} {n}"
        return candidate

    def poll(self, pair_id: str) -> dict | None:
        """``pending`` until approved; then the secret, once. None if unknown or expired."""
        with self._lock:
            entry = self._pending.get(pair_id)
            if not entry or time.monotonic() - entry["created"] > PAIRING_TTL:
                return None
            if entry["secret"] is None:
                return {"status": "pending"}
            del self._pending[pair_id]
            return {"status": "approved", "secret": entry["secret"], "name": entry["name"]}

    def devices(self) -> list[dict]:
        with self._lock:
            return [{"id": k, "name": v["name"], "paired": v["paired"]} for k, v in self._devices.items()]

    def remove(self, device_id: str) -> None:
        with self._lock:
            if self._devices.pop(device_id, None) is not None:
                self._save()

    def device(self, secret: str | None) -> str | None:
        """The paired Apple TV's name for this secret, or None."""
        if not secret:
            return None
        wanted = _hash(secret)
        with self._lock:
            for device in self._devices.values():
                if secrets.compare_digest(device["hash"], wanted):
                    return device["name"]
        return None


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
        "X-Plex-Client-Identifier": f"moviebook-{player}", "X-Plex-Product": "Marquee",
        "X-Plex-Platform": "iOS", "X-Plex-Device": "Apple TV", "X-Plex-Token": token,
    }
    return f"{plex_url}/video/:/transcode/universal/start.m3u8?{urlencode(params)}"


def jellyfin_hls_url(jellyfin_url: str, api_key: str, item_id: str, start: int, player: str) -> str:
    """Jellyfin's HLS stream for a movie: HEVC/H.264 copied into fMP4 segments where possible.

    The key goes in ``ApiKey``: current Jellyfin answers 401 to the old ``api_key``.
    """
    params = {
        "MediaSourceId": item_id, "DeviceId": f"moviebook-{player}", "PlaySessionId": uuid.uuid4().hex,
        "ApiKey": api_key, "VideoCodec": "hevc,h264", "AudioCodec": "aac,ac3,eac3",
        "SegmentContainer": "mp4", "TranscodingMaxAudioChannels": 6, "MaxStreamingBitrate": 120_000_000,
        "StartTimeTicks": int(start) * 10_000_000,
    }
    return f"{jellyfin_url}/Videos/{item_id}/master.m3u8?{urlencode(params)}"
