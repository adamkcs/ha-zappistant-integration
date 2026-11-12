"""Zappistant: weekly charge scheduling for a myenergi zappi."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .controller import ScheduleController
from .websocket_api import async_register_websocket_commands

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type ZappistantConfigEntry = ConfigEntry[ScheduleController]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_websocket_commands(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ZappistantConfigEntry) -> bool:
    controller = ScheduleController(hass, entry)
    await controller.async_start()
    entry.runtime_data = controller
    # The websocket commands look the controller up here; there is only ever one entry.
    hass.data[DOMAIN] = controller
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ZappistantConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
        hass.data.pop(DOMAIN, None)
    return unloaded


async def _async_reload(hass: HomeAssistant, entry: ZappistantConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
