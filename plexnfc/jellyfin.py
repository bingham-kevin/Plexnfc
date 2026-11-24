"""Jellyfin: talk to a Jellyfin server that is a movie or music source (see sources.py).
Its movies play in the Marquee app, streamed through this server."""
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
