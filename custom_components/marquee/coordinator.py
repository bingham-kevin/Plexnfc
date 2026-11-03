"""Keep up with Marquee players: a request the server holds open until one of them changes."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import WAIT, MarqueeAPI, MarqueeAuthError, MarqueeError
from .const import DOMAIN, EVENT

_LOGGER = logging.getLogger(__name__)
RETRY = 10  # seconds between tries while the server can't be reached


def transition(before: dict | None, after: dict) -> str | None:
    """What a player just did, as a trigger type: started, paused, resumed or stopped."""
    if before is None:
        return None
    was, now = before["state"], after["state"]
    same_movie = before.get("key") == after.get("key")
    if now == "playing" and (was == "idle" or not same_movie):
        return "started"
    if now == "playing" and was == "paused":
        return "resumed"
    if now == "paused" and was == "playing" and same_movie:
        return "paused"
    if now == "idle" and was != "idle":
        return "stopped"
    return None


class MarqueeCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Marquee players by the server's device ID, and when that was last heard."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: MarqueeAPI) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=None)
        self.api = api
        self.tag: str | None = None
        self.updated: datetime = dt_util.utcnow()

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            return self._take(await self.api.players())
        except MarqueeAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MarqueeError as err:
            raise UpdateFailed(str(err)) from err

    def _take(self, answer: dict) -> dict[str, dict[str, Any]]:
        self.tag = answer["tag"]
        self.updated = dt_util.utcnow()
        return {p["device_id"]: p for p in answer["players"]}

    async def listen(self) -> None:
        """Run for as long as the entry's loaded: wait for changes, fire triggers, update entities."""
        while True:
            try:
                answer = await self.api.players(self.tag, wait=WAIT)
            except MarqueeAuthError as err:
                _LOGGER.warning("%s", err)
                self.async_set_update_error(err)
                self.config_entry.async_start_reauth(self.hass)
                return
            except MarqueeError as err:
                if self.last_update_success:
                    _LOGGER.warning("%s; trying again every %s seconds", err, RETRY)
                self.async_set_update_error(err)
                self.tag = None  # answer straight away once it's back
                await asyncio.sleep(RETRY)
                continue
            before = self.data or {}
            players = self._take(answer)
            self._fire_triggers(before, players)
            self.async_set_updated_data(players)

    def _fire_triggers(self, before: dict[str, dict], after: dict[str, dict]) -> None:
        devices = dr.async_get(self.hass)
        for device_id, player in after.items():
            if not (kind := transition(before.get(device_id), player)):
                continue
            # what stopped is what was playing
            movie = before[device_id] if kind == "stopped" else player
            device = devices.async_get_device(identifiers={(DOMAIN, device_id)})
            self.hass.bus.async_fire(EVENT, {
                "device_id": device.id if device else None,
                "type": kind,
                "player": player["name"],
                "title": movie.get("title"),
                "key": movie.get("key"),
            })
