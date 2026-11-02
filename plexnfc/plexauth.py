"""Sign in with Plex, so nobody has to dig out a Plex token by hand.

This is Plex's PIN flow: we ask plex.tv for a PIN, send the person to
app.plex.tv to approve it, then poll the PIN until plex.tv attaches a token.
The token is saved to ``auth_file`` (in the app's /data folder when running
in Home Assistant) and never shown in a page.
"""
from __future__ import annotations

import json
import os
import uuid
from urllib.parse import urlencode

import requests

PLEX_TV = "https://plex.tv/api/v2"
PRODUCT = "plexnfc"


def _load(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(path: str, data: dict) -> None:
    tmp = f"{path}.tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def saved_token(path: str) -> str:
    return _load(path).get("token", "")


def client_id(path: str) -> str:
    """A stable ID for this install. Plex lists it under Authorized Devices."""
    data = _load(path)
    if not data.get("client_id"):
        data["client_id"] = str(uuid.uuid4())
        _save(path, data)
    return data["client_id"]


def _headers(path: str) -> dict:
    return {"Accept": "application/json", "X-Plex-Product": PRODUCT,
            "X-Plex-Client-Identifier": client_id(path)}


def start_pin(path: str) -> tuple[int, str]:
    """Ask plex.tv for a new PIN. Returns (pin id, auth URL to send the person to)."""
    r = requests.post(f"{PLEX_TV}/pins", params={"strong": "true"}, headers=_headers(path), timeout=15)
    r.raise_for_status()
    pin = r.json()
    query = urlencode({"clientID": client_id(path), "code": pin["code"],
                       "context[device][product]": PRODUCT})
    return pin["id"], f"https://app.plex.tv/auth#?{query}"


def check_pin(path: str, pin_id: int) -> str | None:
    """Return the token once the PIN has been approved (and save it), else None."""
    r = requests.get(f"{PLEX_TV}/pins/{pin_id}", headers=_headers(path), timeout=15)
    r.raise_for_status()
    token = r.json().get("authToken")
    if token:
        data = _load(path)
        data["token"] = token
        _save(path, data)
    return token or None


def forget_token(path: str) -> None:
    data = _load(path)
    if data.pop("token", None) is not None:
        _save(path, data)
