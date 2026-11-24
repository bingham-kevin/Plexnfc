"""Marquee: movies playing in the Marquee app, as media players with triggers.

Pairs with Marquee Server (or the plexnfc Home Assistant app) the way Marquee
does: you approve a code in the server's control panel. Each Marquee player
(an Apple TV, iPhone or iPad) becomes a media player, and the "started",
"paused", "resumed" and "stopped" triggers let automations follow the movie.
With Music Assistant, cards can also play music, and cards read by Home Assistant's
own NFC readers work as on a Movie Remote (see music.py).
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MarqueeAPI
from .const import CONF_SECRET
from .coordinator import MarqueeCoordinator
from .music import MusicLink

PLATFORMS = [Platform.MEDIA_PLAYER]

type MarqueeConfigEntry = ConfigEntry[MarqueeCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: MarqueeConfigEntry) -> bool:
    api = MarqueeAPI(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_SECRET])
    coordinator = MarqueeCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(hass, coordinator.listen(), "marquee listen")
    # Cards linked to music: the server's jobs for Music Assistant; and cards read by NFC
    # readers in Home Assistant (a Tessera screen, a phone), sent on to Marquee
    music = MusicLink(hass, api)
    entry.async_create_background_task(hass, music.run(), "marquee music")
    entry.async_on_unload(hass.bus.async_listen("tag_scanned", music.tag_scanned))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MarqueeConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
