"""Base entity for Zappistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .controller import ScheduleController


class ZappistantEntity(Entity):
    """Entity that refreshes whenever the controller updates."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, controller: ScheduleController, key: str) -> None:
        self._controller = controller
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name="Zappi schedule")

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._controller.async_add_listener(self.async_write_ha_state))
