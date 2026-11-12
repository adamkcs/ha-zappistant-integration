"""Schedule status sensor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZappistantConfigEntry
from .entity import ZappistantEntity
from .planner import (
    STATE_CHARGING,
    STATE_DISABLED,
    STATE_DONE,
    STATE_ENDED,
    STATE_IDLE,
    STATE_OCTOPUS,
    STATE_OVERRUN,
    STATE_WAITING,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ZappistantConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([ScheduleStatusSensor(entry, entry.runtime_data, "schedule_status")])


class ScheduleStatusSensor(ZappistantEntity, SensorEntity):
    """What the schedule is doing, with the current run's progress as attributes."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        STATE_DISABLED,
        STATE_IDLE,
        STATE_WAITING,
        STATE_CHARGING,
        STATE_OVERRUN,
        STATE_DONE,
        STATE_ENDED,
        STATE_OCTOPUS,
    ]

    @property
    def native_value(self) -> str:
        return self._controller.decision.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes = self._controller.status()
        attributes.pop("state")
        return attributes
