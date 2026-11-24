"""A switch for each speaker a reader could play on: on means its music plays there too."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MarqueeConfigEntry
from .reader import ReaderEntity, add_reader_entities


async def async_setup_entry(hass: HomeAssistant, entry: MarqueeConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    add_reader_entities(coordinator, entry, lambda reader_id, reader: [
        ReaderSpeaker(coordinator, reader_id, choice) for choice in reader.get("choices") or []
    ], async_add_entities)


class ReaderSpeaker(ReaderEntity, SwitchEntity):
    _attr_icon = "mdi:speaker"

    def __init__(self, coordinator, reader_id: str, choice: dict) -> None:
        super().__init__(coordinator, reader_id, f"speaker-{choice['id']}")
        self.speaker = choice["id"]
        self._attr_name = choice["name"]

    @property
    def _choice(self) -> dict | None:
        return next((c for c in self.reader.get("choices") or [] if c["id"] == self.speaker), None)

    @property
    def available(self) -> bool:
        return super().available and self._choice is not None

    @property
    def is_on(self) -> bool:
        return self.speaker in self.reader.get("speakers", [])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"default": self.speaker in self.reader.get("default", [])}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.act({"action": "speaker", "speaker": self.speaker, "on": True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.act({"action": "speaker", "speaker": self.speaker, "on": False})
