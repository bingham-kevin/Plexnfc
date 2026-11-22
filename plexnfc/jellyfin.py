"""Jellyfin: play on a Jellyfin player (e.g. Swiftfin on Apple TV), and talk to a Jellyfin
server that is a movie source (see sources.py).

For a Plex movie, Plex says which movie a tag is (title, year and TMDB/IMDb ID). The
movie is then found in Jellyfin by title and year, checked against that ID, and
started on the Jellyfin player through Jellyfin's session API, which Swiftfin
supports even though Plex for Apple TV doesn't.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

TICKS_PER_SECOND = 10_000_000
PROVIDERS = {"tmdb": "Tmdb", "imdb": "Imdb", "tvdb": "Tvdb"}


class JellyfinError(Exception):
    pass


DEVICE_ID = "marquee-server"


def auth_header(token: str | None = None) -> str:
    """Jellyfin's Authorization header. A user's sign-in token (unlike an API key) wants
    the client named alongside it."""
    header = f'MediaBrowser Client="Marquee", Device="Marquee Server", DeviceId="{DEVICE_ID}", Version="1.0"'
    return f'{header}, Token="{token}"' if token else header


def sign_in(url: str, username: str, password: str) -> dict:
    """Sign in to Jellyfin as a user: ``{"token", "user_id", "user"}``. The password is
    only passed on to Jellyfin, never kept."""
    try:
        r = requests.post(f"{url.rstrip('/')}/Users/AuthenticateByName", timeout=15,
                          json={"Username": username, "Pw": password},
                          headers={"Authorization": auth_header()})
    except requests.RequestException as e:
        raise JellyfinError(f"Couldn't reach Jellyfin: {e}") from e
    if r.status_code in (400, 401):
        raise JellyfinError("Jellyfin didn't accept that user name and password")
    if not r.ok:
        raise JellyfinError(f"Jellyfin sign-in failed: {r.status_code} {r.text[:200]}")
    body = r.json()
    return {"token": body["AccessToken"], "user_id": body["User"]["Id"], "user": body["User"].get("Name", username)}


class Jellyfin:
    """A Jellyfin server, reached with an API key, or with a user's sign-in token
    (``user_id`` set), which sees that user's libraries and watched positions."""

    def __init__(self, url: str, api_key: str, user_id: str | None = None):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id or None

    def _send(self, method: str, path: str, body: dict | None = None, **params) -> requests.Response:
        try:
            r = requests.request(method, f"{self.url}{path}", params=params, json=body, timeout=15,
                                 headers={"Authorization": auth_header(self.api_key) if self.user_id
                                          else f'MediaBrowser Token="{self.api_key}"'})
        except requests.RequestException as e:
            raise JellyfinError(f"Couldn't reach Jellyfin: {e}") from e
        if r.status_code == 401:
            raise JellyfinError("Jellyfin rejected the sign-in: sign in again in the control panel"
                                if self.user_id else "Jellyfin rejected the API key")
        if not r.ok:
            raise JellyfinError(f"Jellyfin {path} failed: {r.status_code} {r.text[:200]}")
        return r

    def _request(self, method: str, path: str, body: dict | None = None, **params):
        r = self._send(method, path, body, **params)
        return r.json() if r.content else None

    def get(self, path: str, **params):
        return self._request("GET", path, **params)

    def image(self, item_id: str, width: int | None = None, height: int | None = None) -> tuple[bytes, str] | None:
        """An item's poster (its primary image) and its type, or None if it has none."""
        params = {"maxWidth": width, "maxHeight": height} if width and height else {}
        try:
            r = self._send("GET", f"/Items/{item_id}/Images/Primary", **params)
        except JellyfinError as e:
            if " 404 " in str(e):
                return None
            raise
        return r.content, r.headers.get("Content-Type", "image/jpeg")

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
