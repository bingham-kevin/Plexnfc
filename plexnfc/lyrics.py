"""Lyrics for what's playing, for Marquee's Now Playing screen.

Where they come from, in order:

- the music's own source: a ``.lrc`` (or ``.txt``) file beside a track in a music folder,
  Plex's lyrics (a sidecar file, or Plex Pass's), Jellyfin's (10.9 and later), or the
  lyrics Music Assistant found;
- else LRCLIB (lrclib.net), a free lyrics database, asked with the track's title, artist,
  album and length only. ``LYRICS_LRCLIB=0`` turns that off.

Timed lyrics (LRC: ``[01:02.50] a line``) come back as lines with a ``time`` in seconds,
so Marquee can follow along; plain lyrics have no times.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import OrderedDict

import requests

log = logging.getLogger(__name__)

LRCLIB = "https://lrclib.net/api/get"
_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_META = re.compile(r"^\[[a-z]{2,8}:.*\]$", re.I)  # [ar: ...], [offset: ...]


def parse(text: str | None) -> dict | None:
    """``{"synced": bool, "lines": [{"time": seconds or None, "text"}]}`` from LRC or plain text."""
    if not text or not text.strip():
        return None
    timed, plain = [], []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if _META.match(line) and not _TAG.match(line):
            continue
        stamps = list(_TAG.finditer(line))
        if stamps and stamps[0].start() == 0:
            words = _TAG.sub("", line).strip()
            for m in stamps:
                fraction = m.group(3) or "0"
                seconds = int(m.group(1)) * 60 + int(m.group(2)) + int(fraction) / (10 ** len(fraction))
                timed.append({"time": round(seconds, 2), "text": words})
        else:
            plain.append({"time": None, "text": line})
    if timed:
        timed.sort(key=lambda l: l["time"])
        return {"synced": True, "lines": timed}
    while plain and not plain[0]["text"]:
        plain.pop(0)
    while plain and not plain[-1]["text"]:
        plain.pop()
    return {"synced": False, "lines": plain} if plain else None


def from_lines(lines: list[tuple[float | None, str]]) -> dict | None:
    """Lyrics from ``(seconds or None, text)`` pairs (Jellyfin's)."""
    if not lines:
        return None
    synced = all(t is not None for t, _ in lines)
    return {"synced": synced, "lines": [{"time": round(t, 2) if synced else None, "text": x} for t, x in lines]}


class Lyrics:
    """Finds lyrics for tracks, and remembers what it found (or that there were none)."""

    KEEP = 300  # tracks remembered
    NONE_FOR = 6 * 3600  # how long "no lyrics" is remembered

    def __init__(self, library, use_lrclib: bool | None = None):
        self.library = library  # musiclib.MusicLibrary
        self.use_lrclib = (os.environ.get("LYRICS_LRCLIB", "1").lower() not in ("0", "false", "no")
                           if use_lrclib is None else use_lrclib)
        self._lock = threading.Lock()
        self._found: OrderedDict[str, tuple[float, dict | None]] = OrderedDict()
        self._looking: set[str] = set()

    def for_track(self, track: dict, own: dict | None = None) -> dict | None:
        """Lyrics for ``track`` (a library track, or just ``name``, ``artist``, ``album`` and
        ``duration`` for Music Assistant's). ``own`` is lyrics already in hand (Music
        Assistant's), tried first."""
        key = self._key(track)
        with self._lock:
            hit = self._found.get(key)
            if hit and (hit[1] is not None or time.monotonic() - hit[0] < self.NONE_FOR):
                self._found.move_to_end(key)
                return hit[1]
        found = own or self._from_source(track) or self._from_lrclib(track)
        with self._lock:
            self._found[key] = (time.monotonic(), found)
            while len(self._found) > self.KEEP:
                self._found.popitem(last=False)
        return found

    def lookup(self, track: dict, own: dict | None = None) -> tuple[bool, dict | None]:
        """Without waiting: ``(True, lyrics or None)`` if they're known, else ``(False, None)``
        while they're looked for in the background."""
        key = self._key(track)
        with self._lock:
            hit = self._found.get(key)
            if hit and (hit[1] is not None or time.monotonic() - hit[0] < self.NONE_FOR):
                return True, hit[1]
            if own is not None:
                self._found[key] = (time.monotonic(), own)
                return True, own
            if key in self._looking:
                return False, None
            self._looking.add(key)

        def look():
            try:
                self.for_track(track)
            finally:
                with self._lock:
                    self._looking.discard(key)
        threading.Thread(target=look, daemon=True).start()
        return False, None

    @staticmethod
    def _key(track: dict) -> str:
        return track.get("id") or "|".join(str(track.get(k) or "") for k in ("artist", "album", "name"))

    def _from_source(self, track: dict) -> dict | None:
        if not track.get("id") or not track.get("source"):
            return None
        try:
            source = self.library.source(track["source"])
            return source.lyrics(track)
        except Exception as e:  # noqa: BLE001 - a source without lyrics is no problem
            log.debug("No lyrics from the source for %s: %s", track.get("name"), e)
            return None

    def _from_lrclib(self, track: dict) -> dict | None:
        if not self.use_lrclib or not track.get("name") or not track.get("artist") or track.get("book"):
            return None
        params = {"track_name": track["name"], "artist_name": track["artist"]}
        if track.get("album"):
            params["album_name"] = track["album"]
        if track.get("duration"):
            params["duration"] = int(track["duration"])
        try:
            r = requests.get(LRCLIB, params=params, timeout=6, headers={"User-Agent": "Marquee Server"})
            if r.status_code != 200:
                return None
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log.debug("LRCLIB: %s", e)
            return None
        if data.get("instrumental"):
            return None
        return parse(data.get("syncedLyrics")) or parse(data.get("plainLyrics"))
