"""Play on a Jellyfin player (e.g. Swiftfin on Apple TV) instead of a Plex one.

Plex still makes the book and says which movie a tag is (title, year and
TMDB/IMDb ID). The movie is then found in Jellyfin by title and year, checked
against that ID, and started on the Jellyfin player through Jellyfin's
session API, which Swiftfin supports even though Plex for Apple TV doesn't.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

TICKS_PER_SECOND = 10_000_000
PROVIDERS = {"tmdb": "Tmdb", "imdb": "Imdb", "tvdb": "Tvdb"}


class JellyfinError(Exception):
    pass


class Jellyfin:
    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.api_key = api_key

    def _request(self, method: str, path: str, body: dict | None = None, **params):
        try:
            r = requests.request(method, f"{self.url}{path}", params=params, json=body, timeout=15,
                                 headers={"Authorization": f'MediaBrowser Token="{self.api_key}"'})
        except requests.RequestException as e:
            raise JellyfinError(f"Couldn't reach Jellyfin: {e}") from e
        if r.status_code == 401:
            raise JellyfinError("Jellyfin rejected the API key")
        if not r.ok:
            raise JellyfinError(f"Jellyfin {path} failed: {r.status_code} {r.text[:200]}")
        return r.json() if r.content else None

    def find_movie(self, title: str, year: int | None, key: str) -> dict:
        """The Jellyfin movie for a tag key like ``tmdb-603``, found by title and year."""
        params = {"Recursive": "true", "IncludeItemTypes": "Movie", "searchTerm": title,
                  "Fields": "ProviderIds", "Limit": 50}
        if year:
            params["years"] = year
        items = (self._request("GET", "/Items", **params) or {}).get("Items", [])
        source, _, ident = key.partition("-")
        provider = PROVIDERS.get(source)
        if provider:
            for item in items:
                if str((item.get("ProviderIds") or {}).get(provider, "")).lower() == ident.lower():
                    return item
        exact = [i for i in items if i.get("Name", "").casefold() == title.casefold()]
        if len(exact) == 1:
            return exact[0]
        raise JellyfinError(f"Couldn't find {title} ({year or '?'}) in Jellyfin")

    def sessions(self) -> list[dict]:
        return self._request("GET", "/Sessions", activeWithinSeconds=960) or []

    def session(self, name: str) -> dict | None:
        """The open Jellyfin app on the player called ``name`` (its device name)."""
        for s in self.sessions():
            if name in (s.get("DeviceName"), s.get("Client")) and s.get("SupportsRemoteControl"):
                return s
        return None

    def players(self) -> list[str]:
        return sorted({s["DeviceName"] for s in self.sessions()
                       if s.get("SupportsRemoteControl") and s.get("DeviceName")})

    def play(self, session: dict, item_id: str, start_seconds: int | None = 0) -> None:
        # Subtitle index -1 means none: without one, Swiftfin turns a subtitle track on.
        params = {"playCommand": "PlayNow", "itemIds": item_id, "subtitleStreamIndex": -1}
        if start_seconds is not None:
            params["startPositionTicks"] = start_seconds * TICKS_PER_SECOND
        self._request("POST", f"/Sessions/{session['Id']}/Playing", **params)

    def subtitles_off(self, session: dict) -> bool:
        """Ask a playing app to turn subtitles off. False if it doesn't support that."""
        if "SetSubtitleStreamIndex" not in (session.get("SupportedCommands") or []):
            return False
        self._request("POST", f"/Sessions/{session['Id']}/Command",
                      body={"Name": "SetSubtitleStreamIndex", "Arguments": {"Index": "-1"}})
        return True

    def command(self, session: dict, command: str) -> None:
        """``Pause``, ``Unpause`` or ``Stop``."""
        self._request("POST", f"/Sessions/{session['Id']}/Playing/{command}")

    def seek(self, session: dict, seconds: int) -> None:
        self._request("POST", f"/Sessions/{session['Id']}/Playing/Seek",
                      seekPositionTicks=seconds * TICKS_PER_SECOND)


def tag_key(item: dict) -> str | None:
    """The tag key (``tmdb-603``) for a Jellyfin item, if it has a known ID."""
    ids = {k.lower(): v for k, v in (item.get("ProviderIds") or {}).items()}
    for source in PROVIDERS:
        if ids.get(source):
            return f"{source}-{ids[source]}"
    return None
