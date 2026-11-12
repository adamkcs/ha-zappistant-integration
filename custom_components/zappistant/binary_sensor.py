"""Charge slot sensor, in the shape Predbat reads car charging plans from."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import ZappistantConfigEntry
from .entity import ZappistantEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ZappistantConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([ChargeSlotSensor(entry, entry.runtime_data, "charge_slot")])


class ChargeSlotSensor(ZappistantEntity, BinarySensorEntity):
    """On during a planned charge; the plan for the next 48 hours is in planned_dispatches.

    Point Predbat's octopus_intelligent_slot at this entity. Each dispatch has the source
    "charger-schedule", which Predbat treats as car load, not as a cheap tariff slot.
    """

    _attr_icon = "mdi:car-clock"

    @property
    def is_on(self) -> bool:
        now = dt_util.now()
        return any(
            datetime.fromisoformat(slot["start"]) <= now < datetime.fromisoformat(slot["end"])
            for slot in self._controller.planned_dispatches()
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "planned_dispatches": self._controller.planned_dispatches(),
            "completed_dispatches": [],
            "charge_point_power_in_kw": self._controller.charge_rate,
        }
