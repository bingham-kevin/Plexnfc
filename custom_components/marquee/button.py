"""A reader's Restore default and Make default buttons."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MarqueeConfigEntry
from .reader import ReaderEntity, add_reader_entities

BUTTONS = {
    "restore": ("Restore default speakers", "mdi:restore"),
    "make_default": ("Make these speakers the default", "mdi:content-save"),
}


async def async_setup_entry(hass: HomeAssistant, entry: MarqueeConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    add_reader_entities(coordinator, entry, lambda reader_id, reader: [
        ReaderButton(coordinator, reader_id, action) for action in BUTTONS
    ], async_add_entities)


class ReaderButton(ReaderEntity, ButtonEntity):
    def __init__(self, coordinator, reader_id: str, action: str) -> None:
        super().__init__(coordinator, reader_id, action)
        self.action = action
        self._attr_name, self._attr_icon = BUTTONS[action]

    async def async_press(self) -> None:
        await self.act({"action": self.action})
