"""Constants for Zappistant."""

DOMAIN = "zappistant"
MYENERGI_DOMAIN = "myenergi"

CONF_MODE_ENTITY = "mode_entity"
CONF_SESSION_ENTITY = "session_entity"
CONF_PLUG_ENTITY = "plug_entity"
CONF_STATUS_ENTITY = "status_entity"
CONF_SWITCH_TO_STOPPED = "switch_to_stopped"
CONF_CHARGE_RATE = "charge_rate"

DEFAULT_CHARGE_RATE = 7.0

# Who decides when the car charges: Zappistant boosting the zappi itself, or Intelligent
# Octopus Go, with Zappistant only setting Octopus's charge target and ready-by time.
CONF_CONTROL_MODE = "control_mode"
MODE_ZAPPI = "zappi"
MODE_OCTOPUS = "octopus"

OCTOPUS_DOMAIN = "octopus_energy"
CONF_OCTOPUS_TARGET_ENTITY = "octopus_target_entity"
CONF_OCTOPUS_TIME_ENTITY = "octopus_time_entity"
# The battery size set in the Octopus app, which Octopus turns "% to add" into kWh with.
CONF_OCTOPUS_BATTERY = "octopus_battery_kwh"
DEFAULT_OCTOPUS_BATTERY = 61.1

# Unique id endings of the Octopus Energy integration's Intelligent entities.
OCTOPUS_TARGET_SUFFIX = "_intelligent_charge_target"
OCTOPUS_TIME_SUFFIX = "_intelligent_target_time"

# Predbat counts dispatches with this source as car load only, never as a cheap tariff slot.
DISPATCH_SOURCE = "charger-schedule"

# Entity id suffixes the myenergi integration gives the zappi's sensors.
SENSOR_SUFFIXES = {
    CONF_SESSION_ENTITY: "_charge_added_session",
    CONF_PLUG_ENTITY: "_plug_status",
    CONF_STATUS_ENTITY: "_status",
}
