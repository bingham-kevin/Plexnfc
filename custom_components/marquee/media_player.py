"""Each Marquee player as a media player: what's playing, with pause, play, stop and play media."""
from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MarqueeConfigEntry
from .api import MarqueeError
from .const import DOMAIN
from .coordinator import MarqueeCoordinator

STATES = {"playing": MediaPlayerState.PLAYING, "paused": MediaPlayerState.PAUSED}


async def async_setup_entry(hass: HomeAssistant, entry: MarqueeConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    added: set[str] = set()

    @callback
    def add_new_players() -> None:
        new = [device_id for device_id in coordinator.data if device_id not in added]
        added.update(new)
        async_add_entities(MarqueePlayer(coordinator, device_id) for device_id in new)

    add_new_players()
    entry.async_on_unload(coordinator.async_add_listener(add_new_players))


class MarqueePlayer(CoordinatorEntity[MarqueeCoordinator], MediaPlayerEntity):
    """Marquee on one Apple TV, iPhone or iPad."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_media_content_type = MediaType.MOVIE
    _attr_media_image_remotely_accessible = False  # the poster's on the home network
    _attr_supported_features = (MediaPlayerEntityFeature.PAUSE | MediaPlayerEntityFeature.PLAY
                                | MediaPlayerEntityFeature.STOP | MediaPlayerEntityFeature.PLAY_MEDIA)

    def __init__(self, coordinator: MarqueeCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self.device_id = device_id
        self._attr_unique_id = device_id
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device_id)}, name=self._player["name"],
                                            manufacturer="Marquee", model="Marquee player",
                                            configuration_url=coordinator.api.url)

    @property
    def _player(self) -> dict[str, Any]:
        return self.coordinator.data.get(self.device_id) or {"state": "idle", "name": ""}

    @property
    def available(self) -> bool:
        # A player unpaired on the server goes unavailable rather than away
        return super().available and self.device_id in self.coordinator.data

    @property
    def state(self) -> MediaPlayerState:
        return STATES.get(self._player["state"], MediaPlayerState.IDLE)

    @property
    def media_content_id(self) -> str | None:
        return self._player.get("key")

    @property
    def media_title(self) -> str | None:
        return self._player.get("title")

    @property
    def media_duration(self) -> int | None:
        return self._player.get("duration") or None

    @property
    def media_position(self) -> int | None:
        return self._player.get("position") if "key" in self._player else None

    @property
    def media_position_updated_at(self):
        return self.coordinator.updated if "key" in self._player else None

    @property
    def media_image_url(self) -> str | None:
        poster = self._player.get("poster")
        return self.coordinator.api.poster_url(poster) if poster else None

    @property
    def _player_id(self) -> str:
        return self._player["id"]

    async def async_media_pause(self) -> None:
        await self._call(self.coordinator.api.control(self._player_id, "pause"))

    async def async_media_play(self) -> None:
        await self._call(self.coordinator.api.control(self._player_id, "play"))

    async def async_media_stop(self) -> None:
        await self._call(self.coordinator.api.control(self._player_id, "stop"))

    async def async_play_media(self, media_type: str, media_id: str, **kwargs: Any) -> None:
        """Start a movie by its key, e.g. tmdb-603 (the end of its tag address), from the beginning."""
        await self._call(self.coordinator.api.play(self._player_id, media_id))

    async def _call(self, request) -> None:
        try:
            await request
        except MarqueeError as err:
            raise HomeAssistantError(str(err)) from err
