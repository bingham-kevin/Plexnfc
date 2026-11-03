"""Marquee: movies playing in the Marquee app, as media players with triggers.

Pairs with Marquee Server (or the plexnfc Home Assistant app) the way Marquee
does: you approve a code in the server's control panel. Each Marquee player
(an Apple TV, iPhone or iPad) becomes a media player, and the "started",
"paused", "resumed" and "stopped" triggers let automations follow the movie.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MarqueeAPI
from .const import CONF_SECRET
from .coordinator import MarqueeCoordinator

PLATFORMS = [Platform.MEDIA_PLAYER]

type MarqueeConfigEntry = ConfigEntry[MarqueeCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: MarqueeConfigEntry) -> bool:
    api = MarqueeAPI(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_SECRET])
    coordinator = MarqueeCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(hass, coordinator.listen(), "marquee listen")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MarqueeConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
