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
from .reader import ReaderEntity, add_reader_entities

STATES = {"playing": MediaPlayerState.PLAYING, "paused": MediaPlayerState.PAUSED}


async def async_setup_entry(hass: HomeAssistant, entry: MarqueeConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    add_reader_entities(coordinator, entry, lambda reader_id, reader: [ReaderPlayer(coordinator, reader_id)],
                        async_add_entities)
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


class ReaderPlayer(ReaderEntity, MediaPlayerEntity):
    """What an NFC reader's speakers are playing (music or a book), to control from the reader's room."""

    _attr_name = None
    _attr_media_image_remotely_accessible = False
    _attr_supported_features = (MediaPlayerEntityFeature.PAUSE | MediaPlayerEntityFeature.PLAY
                                | MediaPlayerEntityFeature.STOP | MediaPlayerEntityFeature.NEXT_TRACK
                                | MediaPlayerEntityFeature.PREVIOUS_TRACK | MediaPlayerEntityFeature.VOLUME_SET)

    def __init__(self, coordinator: MarqueeCoordinator, reader_id: str) -> None:
        super().__init__(coordinator, reader_id, "player")

    @property
    def _now(self) -> dict[str, Any]:
        return self.reader.get("now") or {}

    @property
    def state(self) -> MediaPlayerState:
        return STATES.get(self._now.get("state"), MediaPlayerState.IDLE)

    @property
    def media_content_type(self) -> str:
        return MediaType.PODCAST if self._now.get("book") else MediaType.MUSIC

    @property
    def media_title(self) -> str | None:
        return self._now.get("title") or None

    @property
    def media_artist(self) -> str | None:
        return self._now.get("artist") or None

    @property
    def media_album_name(self) -> str | None:
        return self._now.get("album") or None

    @property
    def media_duration(self) -> int | None:
        return self._now.get("duration") or None

    @property
    def media_position(self) -> int | None:
        return self._now.get("position") if self._now else None

    @property
    def media_position_updated_at(self):
        return self.coordinator.updated if self._now else None

    @property
    def media_image_url(self) -> str | None:
        image = self._now.get("image")
        if not image:
            return None
        return image if image.startswith(("http://", "https://")) else self.coordinator.api.poster_url(image)

    @property
    def volume_level(self) -> float | None:
        volume = self._now.get("volume")
        return volume / 100 if isinstance(volume, (int, float)) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"speakers": self.reader.get("speaker_names") or [],
                "default_speakers": self.reader.get("default_names") or [],
                "is_default": bool(self.reader.get("is_default"))}

    async def async_media_pause(self) -> None:
        await self.act({"action": "pause"})

    async def async_media_play(self) -> None:
        await self.act({"action": "play"})

    async def async_media_stop(self) -> None:
        await self.act({"action": "stop"})

    async def async_media_next_track(self) -> None:
        await self.act({"action": "next"})

    async def async_media_previous_track(self) -> None:
        await self.act({"action": "previous"})

    async def async_set_volume_level(self, volume: float) -> None:
        await self.act({"action": "volume", "level": round(volume * 100)})
