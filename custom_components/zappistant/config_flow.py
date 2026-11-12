"""Config flow for Zappistant."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_CHARGE_RATE,
    CONF_CONTROL_MODE,
    CONF_MODE_ENTITY,
    CONF_OCTOPUS_BATTERY,
    CONF_OCTOPUS_TARGET_ENTITY,
    CONF_OCTOPUS_TIME_ENTITY,
    CONF_PLUG_ENTITY,
    CONF_SESSION_ENTITY,
    CONF_STATUS_ENTITY,
    CONF_SWITCH_TO_STOPPED,
    DEFAULT_CHARGE_RATE,
    DEFAULT_OCTOPUS_BATTERY,
    DOMAIN,
    MODE_OCTOPUS,
    MODE_ZAPPI,
    MYENERGI_DOMAIN,
    OCTOPUS_DOMAIN,
    OCTOPUS_TARGET_SUFFIX,
    OCTOPUS_TIME_SUFFIX,
    SENSOR_SUFFIXES,
)


def _discover_sensors(hass: HomeAssistant, mode_entity: str) -> dict[str, str]:
    """Find the zappi's sensors on the same device as its charge mode select."""
    registry = er.async_get(hass)
    mode = registry.async_get(mode_entity)
    if mode is None or mode.device_id is None:
        return {}
    sensors = [
        entry.entity_id
        for entry in er.async_entries_for_device(registry, mode.device_id)
        if entry.domain == "sensor"
    ]
    found = {}
    for key, suffix in SENSOR_SUFFIXES.items():
        match = next(
            (
                entity_id
                for entity_id in sensors
                # "_plug_status" also ends with "_status".
                if entity_id.endswith(suffix)
                and not (key == CONF_STATUS_ENTITY and entity_id.endswith("_plug_status"))
            ),
            None,
        )
        if match:
            found[key] = match
    return found


def _discover_octopus(hass: HomeAssistant) -> dict[str, str]:
    """Find the Octopus Energy integration's Intelligent charge target and target time entities."""
    found: dict[str, str] = {}
    for entry in er.async_get(hass).entities.values():
        if entry.platform != OCTOPUS_DOMAIN or not entry.unique_id:
            continue
        if entry.unique_id.endswith(OCTOPUS_TARGET_SUFFIX) and entry.domain == "number":
            found.setdefault(CONF_OCTOPUS_TARGET_ENTITY, entry.entity_id)
        elif entry.unique_id.endswith(OCTOPUS_TIME_SUFFIX) and entry.domain in ("time", "select"):
            found.setdefault(CONF_OCTOPUS_TIME_ENTITY, entry.entity_id)
    return found


def _sensor_selector() -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain="sensor", integration=MYENERGI_DOMAIN))


def _charge_rate_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=1, max=22, step=0.1, unit_of_measurement="kW", mode=NumberSelectorMode.BOX
        )
    )


def _control_settings_schema(defaults: dict[str, Any]) -> dict[Any, Any]:
    """The settings shared by setup and the options: who controls charging, and how."""
    return {
        vol.Required(CONF_CONTROL_MODE, default=defaults.get(CONF_CONTROL_MODE, MODE_ZAPPI)): SelectSelector(
            SelectSelectorConfig(
                options=[MODE_ZAPPI, MODE_OCTOPUS],
                translation_key=CONF_CONTROL_MODE,
                mode=SelectSelectorMode.LIST,
            )
        ),
        vol.Optional(
            CONF_SWITCH_TO_STOPPED, default=defaults.get(CONF_SWITCH_TO_STOPPED, True)
        ): BooleanSelector(),
        vol.Optional(
            CONF_CHARGE_RATE, default=defaults.get(CONF_CHARGE_RATE, DEFAULT_CHARGE_RATE)
        ): _charge_rate_selector(),
    }


def _octopus_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_OCTOPUS_TARGET_ENTITY): EntitySelector(
                EntitySelectorConfig(domain="number", integration=OCTOPUS_DOMAIN)
            ),
            vol.Required(CONF_OCTOPUS_TIME_ENTITY): EntitySelector(
                EntitySelectorConfig(domain=["time", "select"], integration=OCTOPUS_DOMAIN)
            ),
            vol.Required(CONF_OCTOPUS_BATTERY): NumberSelector(
                NumberSelectorConfig(
                    min=10, max=200, step=0.1, unit_of_measurement="kWh", mode=NumberSelectorMode.BOX
                )
            ),
        }
    )


def _octopus_suggestions(hass: HomeAssistant, current: dict[str, Any]) -> dict[str, Any]:
    """Prefill the Octopus step: what's set already, else what's found, else a default battery."""
    return {CONF_OCTOPUS_BATTERY: DEFAULT_OCTOPUS_BATTERY, **_discover_octopus(hass), **current}


class ZappistantConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**_discover_sensors(self.hass, user_input[CONF_MODE_ENTITY]), **user_input}
            if all(data.get(key) for key in SENSOR_SUFFIXES):
                self._data = data
                if data[CONF_CONTROL_MODE] == MODE_OCTOPUS:
                    return await self.async_step_octopus()
                return self.async_create_entry(title="Zappi schedule", data=data)
            errors["base"] = "sensors_not_found"

        schema = vol.Schema(
            {
                vol.Required(CONF_MODE_ENTITY): EntitySelector(
                    EntitySelectorConfig(domain="select", integration=MYENERGI_DOMAIN)
                ),
                vol.Optional(CONF_SESSION_ENTITY): _sensor_selector(),
                vol.Optional(CONF_PLUG_ENTITY): _sensor_selector(),
                vol.Optional(CONF_STATUS_ENTITY): _sensor_selector(),
                **_control_settings_schema(user_input or {}),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_octopus(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Zappi schedule", data={**self._data, **user_input})
        return self.async_show_form(
            step_id="octopus",
            data_schema=self.add_suggested_values_to_schema(
                _octopus_schema(), _octopus_suggestions(self.hass, {})
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ZappistantOptionsFlow()


class ZappistantOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    def _current(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._options = user_input
            if user_input[CONF_CONTROL_MODE] == MODE_OCTOPUS:
                return await self.async_step_octopus()
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(_control_settings_schema(self._current()))
        )

    async def async_step_octopus(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self._options, **user_input})
        current = {
            key: value
            for key, value in self._current().items()
            if key in (CONF_OCTOPUS_TARGET_ENTITY, CONF_OCTOPUS_TIME_ENTITY, CONF_OCTOPUS_BATTERY)
        }
        return self.async_show_form(
            step_id="octopus",
            data_schema=self.add_suggested_values_to_schema(
                _octopus_schema(), _octopus_suggestions(self.hass, current)
            ),
        )
