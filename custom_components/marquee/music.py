"""Music cards: Marquee Server asks, Music Assistant plays.

A Movie Remote card can be linked to an album, playlist, artist, track or radio station
in Music Assistant. Marquee Server can reach Music Assistant itself, if it's given its
address and a token; otherwise it goes through here. It has no Home Assistant token, so this integration
keeps a request open to the server (``/ha/music/poll``), sending the Music Assistant
speakers it can see; the server answers with jobs to run here (search the library, or
play something on a speaker) and gets each result back.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import slugify

from .api import MarqueeAPI, MarqueeAuthError, MarqueeError

_LOGGER = logging.getLogger(__name__)

# Fired when a card read by a reader in Home Assistant was linked, played or isn't known,
# with what Marquee made of it, for automations (a sound, a notification...)
CARD_EVENT = "marquee_card"

MUSIC_ASSISTANT = "music_assistant"
SEARCH_TYPES = ["album", "playlist", "artist", "track", "radio"]
RETRY = 30
# A server without music cards answers 404: check again now and then, in case it's updated
NOT_SUPPORTED_RETRY = 600


def music_assistant_entry(hass: HomeAssistant) -> str | None:
    """The loaded Music Assistant config entry's ID, if there is one."""
    for entry in hass.config_entries.async_entries(MUSIC_ASSISTANT):
        if entry.state is ConfigEntryState.LOADED:
            return entry.entry_id
    return None


def speakers(hass: HomeAssistant) -> list[dict[str, str]]:
    """Music Assistant's media players, with the names Home Assistant shows and their
    Music Assistant player IDs."""
    out = []
    for entry in er.async_get(hass).entities.values():
        if entry.platform != MUSIC_ASSISTANT or entry.domain != "media_player" or entry.disabled:
            continue
        state = hass.states.get(entry.entity_id)
        name = (state and state.attributes.get("friendly_name")) or entry.name or entry.original_name
        # Music Assistant's player ID is the entity's unique ID: cards keep that, so they
        # work whether Marquee reaches Music Assistant directly or through here
        out.append({"entity_id": entry.entity_id, "name": name or entry.entity_id, "player_id": entry.unique_id})
    return out


def flatten(found: dict[str, Any]) -> list[dict[str, Any]]:
    """Music Assistant's search answer (lists by type) as one list, best kinds first."""
    items = []
    for kind in ("albums", "playlists", "artists", "tracks", "radio"):
        for item in found.get(kind) or []:
            artists = item.get("artists") or []
            items.append({
                "uri": item.get("uri"),
                "media_type": item.get("media_type"),
                "name": f"{item.get('name')} ({item['version']})" if item.get("version") and item.get("name")
                        else item.get("name"),
                "artist": ", ".join(a.get("name", "") for a in artists if a.get("name")),
                "image": item.get("image") if isinstance(item.get("image"), str) else None,
            })
    return items


def esphome_node(hass: HomeAssistant, device_id: str) -> str | None:
    """The ESPHome node name of a device (what its services are named after, which can
    differ from the device's name), or None if it isn't an ESPHome device."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != "esphome":
            continue
        info = getattr(getattr(entry, "runtime_data", None), "device_info", None)
        name = getattr(info, "name", None) or entry.data.get("device_name")
        return slugify(name or device.name or "")
    return None


class MusicLink:
    """Runs the server's music jobs for as long as the entry's loaded."""

    def __init__(self, hass: HomeAssistant, api: MarqueeAPI) -> None:
        self.hass = hass
        self.api = api

    @callback
    def tag_scanned(self, event: Event) -> None:
        """Home Assistant read an NFC card (``tag_scanned``): ask Marquee what it plays."""
        if tag_id := event.data.get("tag_id"):
            self.hass.async_create_task(self._tap(str(tag_id), event.data.get("device_id")), eager_start=False)

    async def _tap(self, tag_id: str, device_id: str | None) -> None:
        device = dr.async_get(self.hass).async_get(device_id) if device_id else None
        reader = (device.name_by_user or device.name) if device else None
        try:
            answer = await self.api.tap(tag_id, reader, device_id)
        except MarqueeError as err:
            if getattr(err, "status", None) != 404:  # 404: a server without cards from Home Assistant
                _LOGGER.warning("Couldn't ask Marquee about card %s: %s", tag_id, err)
            return
        result = answer.get("result")
        if result == "music" and isinstance(answer.get("play"), dict):
            try:
                await self._do("play", answer["play"])
            except Exception as err:  # noqa: BLE001 - reported in the event
                _LOGGER.warning("Card %s couldn't play: %s", tag_id, err)
                result, answer["message"] = "error", f"Couldn't play {answer.get('title')}: {err}"
        page = (answer.get("reader") or {}).get("page")
        if device_id and page:
            await self._show_page(device_id, int(page))
        self.hass.bus.async_fire(CARD_EVENT, {
            "tag_id": tag_id, "device_id": device_id, "result": result,
            "title": answer.get("title"), "message": answer.get("message"),
        })

    async def _show_page(self, device_id: str, page: int) -> None:
        """Open the reader's controls on its Tessera screen (ESPHome's ``<node>_show_page``)."""
        node = esphome_node(self.hass, device_id)
        if not node or not self.hass.services.has_service("esphome", f"{node}_show_page"):
            _LOGGER.debug("The reader %s has no show_page service", device_id)
            return
        try:
            await self.hass.services.async_call("esphome", f"{node}_show_page", {"page": page}, blocking=True)
        except Exception as err:  # noqa: BLE001 - the card still played
            _LOGGER.warning("Couldn't open page %s on the reader's screen: %s", page, err)

    async def run(self) -> None:
        while True:
            try:
                jobs = await self.api.music_poll(speakers(self.hass), music_assistant_entry(self.hass) is not None)
            except MarqueeAuthError:
                return  # the coordinator asks to pair again
            except MarqueeError as err:
                if getattr(err, "status", None) == 404:
                    _LOGGER.debug("This Marquee Server has no music cards")
                    await asyncio.sleep(NOT_SUPPORTED_RETRY)
                else:
                    await asyncio.sleep(RETRY)
                continue
            for job in jobs:
                self.hass.async_create_task(self._run_job(job), eager_start=False)

    async def _run_job(self, job: dict[str, Any]) -> None:
        try:
            result = await self._do(job.get("kind"), job.get("data") or {})
            answer = {"ok": True, "result": result}
        except Exception as err:  # noqa: BLE001 - every failure goes back to the server
            _LOGGER.warning("Music card %s failed: %s", job.get("kind"), err)
            answer = {"ok": False, "message": str(err) or type(err).__name__}
        try:
            await self.api.music_done(job["id"], answer)
        except MarqueeError as err:
            _LOGGER.debug("Couldn't send a music result: %s", err)

    async def _do(self, kind: str | None, data: dict[str, Any]) -> Any:
        entry_id = music_assistant_entry(self.hass)
        if entry_id is None:
            raise RuntimeError("Music Assistant isn't set up in Home Assistant")
        if kind == "search":
            found = await self.hass.services.async_call(
                MUSIC_ASSISTANT, "search",
                {"config_entry_id": entry_id, "name": str(data.get("query", "")), "media_type": SEARCH_TYPES,
                 "limit": 6},
                blocking=True, return_response=True)
            return flatten(found or {})
        if kind == "play":
            speaker = str(data.get("speaker", ""))
            if self.hass.states.get(speaker) is None:
                raise RuntimeError(f"Home Assistant has no speaker {speaker}")
            await self.hass.services.async_call(
                MUSIC_ASSISTANT, "play_media",
                {"media_id": data["uri"], "media_type": data.get("media_type"), "enqueue": "replace"},
                target={"entity_id": speaker}, blocking=True)
            return None
        raise RuntimeError(f"Unknown music job {kind}")
