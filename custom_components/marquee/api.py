"""Talk to Marquee Server: pairing, players and controls."""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

import aiohttp

# The server holds /ha/players open until something changes, for up to this long
WAIT = 25


class MarqueeError(Exception):
    """The server couldn't be reached, or said no."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class MarqueeAuthError(MarqueeError):
    """The server doesn't know this Home Assistant (any more)."""


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = f"http://{url}"
    if url.count(":") == 1:  # no port: the server's usual one
        url += ":8080"
    return url


class MarqueeAPI:
    def __init__(self, session: aiohttp.ClientSession, url: str, secret: str | None = None) -> None:
        self.session = session
        self.url = normalize_url(url)
        self.secret = secret

    async def _request(self, method: str, path: str, timeout: float = 10, **kwargs: Any) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {self.secret}"} if self.secret else {}
        try:
            async with self.session.request(method, f"{self.url}{path}", headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=timeout), **kwargs) as response:
                data = await response.json(content_type=None) if response.status != 204 else None
                return response.status, data
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise MarqueeError(f"Can't reach Marquee Server at {self.url}: {err or type(err).__name__}") from err

    # ---- pairing ---------------------------------------------------------------------

    async def supports_home_assistant(self) -> bool:
        """Whether the server has the Home Assistant API (older ones answer 404)."""
        status, _ = await self._request("GET", "/ha/players?wait=0")
        return status in (200, 401)

    async def start_pairing(self, name: str) -> dict:
        """Ask to pair; returns ``pair_id`` and the ``code`` to approve on the server."""
        status, data = await self._request("POST", "/tv/pair", json={"name": name, "kind": "homeassistant"})
        if status != 200 or not data:
            raise MarqueeError(f"Marquee Server didn't start pairing (HTTP {status})")
        return data

    async def pairing_secret(self, pair_id: str) -> str | None:
        """The secret once the code's approved, None while it's waiting."""
        status, data = await self._request("GET", f"/tv/pair/{pair_id}")
        if status == 404:
            raise MarqueeAuthError("That pairing code has expired")
        return data.get("secret") if data and data.get("status") == "approved" else None

    # ---- players ---------------------------------------------------------------------

    async def players(self, tag: str | None = None, wait: float = 0) -> dict:
        """``players`` and their ``tag``. With the last ``tag``, waits up to ``wait`` seconds for a change."""
        params = {"wait": str(wait)}
        if tag:
            params["tag"] = tag
        status, data = await self._request("GET", "/ha/players", timeout=wait + 10, params=params)
        if status == 401:
            raise MarqueeAuthError("Marquee Server doesn't know this Home Assistant any more")
        if status != 200:
            raise MarqueeError(f"Marquee Server answered HTTP {status}")
        return data

    async def control(self, player: str, action: str) -> None:
        await self._post("/ha/control", {"player": player, "action": action})

    async def play(self, player: str, key: str, resume: bool = False) -> None:
        await self._post("/ha/play", {"player": player, "key": key, "resume": resume})

    async def audio(self, player: str, url: str, **details: Any) -> None:
        """Play an audio stream on a Marquee, with its title, artist, album, image and duration."""
        await self._post("/ha/audio", {"player": player, "url": url, **details})

    async def _post(self, path: str, body: dict) -> None:
        status, data = await self._request("POST", path, json=body)
        if status == 401:
            raise MarqueeAuthError("Marquee Server doesn't know this Home Assistant any more")
        if status >= 400:
            raise MarqueeError((data or {}).get("message") or f"Marquee Server answered HTTP {status}")

    # ---- music cards (see music.py) -------------------------------------------------

    async def music_poll(self, speakers: list[dict], music_assistant: bool, marquee_players: dict[str, str] | None = None,
                         wait: float = WAIT) -> list[dict]:
        """Tell the server which speakers Music Assistant has (and each Marquee's media player
        here); get the jobs waiting, waiting up to ``wait`` seconds for one."""
        status, data = await self._request("POST", "/ha/music/poll", timeout=wait + 10, params={"wait": str(wait)},
                                           json={"speakers": speakers, "music_assistant": music_assistant,
                                                 "marquee_players": marquee_players or {}})
        if status == 401:
            raise MarqueeAuthError("Marquee Server doesn't know this Home Assistant any more")
        if status != 200 or not isinstance(data, dict):
            raise MarqueeError(f"Marquee Server answered HTTP {status}", status)
        return [j for j in data.get("jobs") or [] if isinstance(j, dict) and "id" in j]

    async def tap(self, uid: str, reader: str | None, reader_id: str | None = None) -> dict:
        """A card read by an NFC reader in Home Assistant (its device ``reader_id`` and name
        ``reader``): what Marquee does with it."""
        status, data = await self._request("POST", "/ha/tap",
                                           json={"uid": uid, "reader": reader, "reader_id": reader_id})
        if status == 401:
            raise MarqueeAuthError("Marquee Server doesn't know this Home Assistant any more")
        if status != 200 or not isinstance(data, dict):
            raise MarqueeError(f"Marquee Server answered HTTP {status}", status)
        return data

    async def reader(self, reader_id: str, body: dict) -> dict:
        """Do something on a reader (choose a speaker, restore or make the default, control what
        plays); returns the reader as it is now."""
        status, data = await self._request("POST", f"/ha/readers/{quote(reader_id, safe='')}", json=body)
        if status == 401:
            raise MarqueeAuthError("Marquee Server doesn't know this Home Assistant any more")
        if status >= 400 or not isinstance(data, dict):
            raise MarqueeError((data or {}).get("message") or f"Marquee Server answered HTTP {status}", status)
        return data.get("reader") or {}

    async def music_done(self, job_id: int, answer: dict) -> None:
        await self._post(f"/ha/music/jobs/{int(job_id)}", answer)

    def poster_url(self, path: str) -> str:
        return f"{self.url}{path}"
