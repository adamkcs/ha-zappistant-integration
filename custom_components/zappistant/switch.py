"""Switch to turn the whole schedule on or off."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZappistantConfigEntry
from .entity import ZappistantEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ZappistantConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([ScheduleEnabledSwitch(entry, entry.runtime_data, "schedule_enabled")])


class ScheduleEnabledSwitch(ZappistantEntity, SwitchEntity):
    _attr_icon = "mdi:calendar-clock"

    @property
    def is_on(self) -> bool:
        return self._controller.schedule["enabled"]

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._controller.async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._controller.async_set_enabled(False)
