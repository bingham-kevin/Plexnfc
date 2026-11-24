"""NFC readers in Home Assistant (a Tessera screen, say), with Marquee's controls for them.

Marquee Server gives each reader a default output (chosen in its control panel) and the
speakers it plays on now. Here each reader gets a device with:

- a media player: what's playing on its speakers, with pause, play, skip, stop and volume;
- a switch for each speaker, to add or take it away (remembered until changed again);
- buttons to restore the default speakers, or make the ones it uses now the default.

A Tessera screen can show these on a page of its own, which Marquee opens when a card is
read there (see music.py).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import MarqueeError
from .const import DOMAIN
from .coordinator import MarqueeCoordinator


def reader_device(coordinator: MarqueeCoordinator, reader: dict) -> DeviceInfo:
    """The reader's Marquee controls, as a device of their own linked to the reader's."""
    info = DeviceInfo(identifiers={(DOMAIN, reader["id"])}, name=f"{reader['name']} Marquee",
                      manufacturer="Marquee", model="NFC reader controls",
                      configuration_url=coordinator.api.url)
    ha_device = reader["id"].removeprefix("ha:")
    device = dr.async_get(coordinator.hass).async_get(ha_device)
    if device and (via := next(iter(device.identifiers), None)):
        info["via_device"] = via
    return info


def add_reader_entities(coordinator: MarqueeCoordinator, entry: ConfigEntry,
                        make: Callable[[str, dict], list], async_add_entities) -> None:
    """Add ``make(reader_id, reader)``'s entities for each reader, and for new ones as they come."""
    added: set[str] = set()

    @callback
    def add_new() -> None:
        new = []
        for reader_id, reader in coordinator.readers.items():
            for entity in make(reader_id, reader):
                if entity.unique_id not in added:
                    added.add(entity.unique_id)
                    new.append(entity)
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))


class ReaderEntity(CoordinatorEntity[MarqueeCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: MarqueeCoordinator, reader_id: str, key: str) -> None:
        super().__init__(coordinator)
        self.reader_id = reader_id
        self._attr_unique_id = f"{reader_id}-{key}"
        self._attr_device_info = reader_device(coordinator, coordinator.readers[reader_id])

    @property
    def reader(self) -> dict[str, Any]:
        return self.coordinator.readers.get(self.reader_id) or {"speakers": [], "default": [], "choices": []}

    @property
    def available(self) -> bool:
        return super().available and self.reader_id in self.coordinator.readers

    async def act(self, body: dict) -> None:
        try:
            self.coordinator.take_reader(await self.coordinator.api.reader(self.reader_id, body))
        except MarqueeError as err:
            raise HomeAssistantError(str(err)) from err
