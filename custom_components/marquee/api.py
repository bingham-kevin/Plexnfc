"""Talk to Marquee Server (or the plexnfc Home Assistant app): pairing, players and controls."""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

# The server holds /ha/players open until something changes, for up to this long
WAIT = 25


class MarqueeError(Exception):
    """The server couldn't be reached, or said no."""


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

    async def _post(self, path: str, body: dict) -> None:
        status, data = await self._request("POST", path, json=body)
        if status == 401:
            raise MarqueeAuthError("Marquee Server doesn't know this Home Assistant any more")
        if status >= 400:
            raise MarqueeError((data or {}).get("message") or f"Marquee Server answered HTTP {status}")

    def poster_url(self, path: str) -> str:
        return f"{self.url}{path}"
