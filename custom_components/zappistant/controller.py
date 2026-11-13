"""Runs the weekly charge plan against the myenergi zappi entities."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, timedelta
import logging
import math
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

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
    DISPATCH_SOURCE,
    DOMAIN,
    MODE_OCTOPUS,
    MODE_ZAPPI,
    MYENERGI_DOMAIN,
)
from .planner import (
    BOOST_MANUAL,
    BOOST_SMART,
    STATE_DISABLED,
    STATE_OCTOPUS,
    Boost,
    Decision,
    Run,
    advance,
    empty_schedule,
    next_interval,
    octopus_percent,
    octopus_upcoming,
    planned_slots,
    ready_by_time,
    upcoming,
    validate_override,
    validate_schedule,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
TICK = timedelta(minutes=1)
# The myenergi integration polls the cloud, so "Boosting" can take a while to show up.
BOOST_RETRY = timedelta(minutes=5)
MIN_BOOST_KWH = 1
MAX_BOOST_KWH = 99
UNPLUGGED = {"", "EV Disconnected"}
# A zappi only accepts a boost in these modes; the myenergi integration silently ignores it otherwise.
BOOST_MODES = ("Eco", "Eco+")
# A zappi takes a while to apply a mode change, and the myenergi integration only reads its real
# mode once a minute, so a boost sent straight after switching can be lost. Wait longer than one
# poll, then check the zappi reports the new mode before boosting.
MODE_SETTLE = timedelta(seconds=75)
# A boost now that the zappi hasn't been running for this long has finished or been stopped.
MANUAL_BOOST_GRACE = timedelta(minutes=10)
# Octopus's entities update from its API, so give a change time to show before setting it again.
OCTOPUS_RETRY = timedelta(minutes=5)


class ScheduleController:
    """Keeps the schedule, tracks progress and starts or stops boosts."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass

        def option(key: str, default: Any = None) -> Any:
            return entry.options.get(key, entry.data.get(key, default))

        self.mode_entity: str = entry.data[CONF_MODE_ENTITY]
        self.session_entity: str = entry.data[CONF_SESSION_ENTITY]
        self.plug_entity: str = entry.data[CONF_PLUG_ENTITY]
        self.status_entity: str = entry.data[CONF_STATUS_ENTITY]
        self.switch_to_stopped: bool = option(CONF_SWITCH_TO_STOPPED, True)
        self.charge_rate: float = option(CONF_CHARGE_RATE, DEFAULT_CHARGE_RATE)
        self.control_mode: str = option(CONF_CONTROL_MODE, MODE_ZAPPI)
        self.octopus_target_entity: str | None = option(CONF_OCTOPUS_TARGET_ENTITY)
        self.octopus_time_entity: str | None = option(CONF_OCTOPUS_TIME_ENTITY)
        self.octopus_battery: float = option(CONF_OCTOPUS_BATTERY, DEFAULT_OCTOPUS_BATTERY)
        # What Octopus should currently be set to, for the status sensor and the card.
        self.octopus_target: dict[str, Any] | None = None
        self._octopus_last_set: tuple[Any, ...] | None = None
        self.schedule: dict[str, Any] = empty_schedule()
        # Boosts started from the card, kept so the charging plan includes them.
        self.boosts: list[Boost] = []
        # One-off adjustments keyed by ISO date, e.g. a different start % for one evening.
        self.overrides: dict[str, dict[str, Any]] = {}
        self.run: Run | None = None
        self.decision = Decision(STATE_DISABLED)
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        # Only stop boosts this controller started, never one the user started by hand.
        self._boost_started = False
        self._last_boost_call: datetime | None = None
        self._mode_switched_at: datetime | None = None
        # What Zappistant last asked the zappi to do, shown on the status sensor.
        self.last_action: dict[str, str] | None = None
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []

    async def async_start(self) -> None:
        data = await self._store.async_load() or {}
        if data.get("schedule"):
            try:
                self.schedule = validate_schedule(data["schedule"])
            except (ValueError, KeyError, TypeError):
                _LOGGER.warning("Stored schedule is invalid, starting with an empty one")
        self.overrides = data.get("overrides") or {}
        self.boosts = [Boost.from_dict(boost) for boost in data.get("boosts") or []]
        self.run = Run.from_dict(data.get("run"))
        self._boost_started = data.get("boost_started", False)

        self._unsubs.append(async_track_time_interval(self.hass, self._async_tick, TICK))
        watched = [self.session_entity, self.plug_entity, self.status_entity]
        if self.octopus_mode:
            # Put Octopus back to the plan if its target is changed elsewhere, e.g. in its app.
            watched += [e for e in (self.octopus_target_entity, self.octopus_time_entity) if e]
        self._unsubs.append(async_track_state_change_event(self.hass, watched, self._async_state_changed))
        await self.async_update()

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self._store.async_save(self._data())

    @callback
    def async_add_listener(self, update_callback: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(update_callback)
        return lambda: self._listeners.remove(update_callback)

    async def async_set_schedule(self, schedule: dict[str, Any]) -> dict[str, Any]:
        """Replace the schedule. Raises ValueError (or KeyError/TypeError) for invalid input."""
        self.schedule = validate_schedule(schedule)
        await self.async_update()
        return self.schedule

    async def async_set_override(self, day: str, override: dict[str, Any] | None) -> None:
        """Adjust one date's charge, or clear the adjustment with None. Raises ValueError."""
        day = date.fromisoformat(day).isoformat()
        if override is None:
            self.overrides.pop(day, None)
        else:
            self.overrides[day] = validate_override(override)
        await self.async_update()

    async def async_boost(
        self, kind: str, kwh: float, ready_by: str | None = None, switch_to_stopped: bool = False
    ) -> None:
        """Start a boost now or a smart boost, and add it to the charging plan.

        switch_to_stopped: once the boost is over, switch the zappi to Stopped so it ends at the
        boost's kWh rather than carrying on in Eco+ (never in Octopus mode, which needs Eco+).
        """
        now = dt_util.now()
        if kind == BOOST_SMART and not ready_by:
            raise ValueError("Smart boost needs a ready-by time")
        deadline = ready_by_time(now, ready_by) if kind == BOOST_SMART and ready_by else None
        # A new boost replaces the zappi's previous one. It's sent to the zappi straight away if it
        # can boost now, otherwise by async_update() once it has switched to Eco+.
        self.boosts = [
            Boost(
                kind=kind,
                kwh=kwh,
                started=now.isoformat(),
                ready_by=deadline.isoformat() if deadline else None,
                last_session=self._float_state(self.session_entity),
                stop_after=switch_to_stopped and not self.octopus_mode,
                sent=False,
            )
        ]
        await self.async_update()

    async def async_stop_boost(self) -> None:
        await self._async_call(MYENERGI_DOMAIN, "myenergi_stop_boost", {})
        if any(boost.stop_after for boost in self.boosts):
            await self._async_set_stopped()
        self.boosts = []
        await self.async_update()

    @property
    def octopus_mode(self) -> bool:
        return self.control_mode == MODE_OCTOPUS

    def planned_dispatches(self) -> list[dict[str, Any]]:
        """Expected charging in the format Predbat reads from a car charging slot sensor."""
        # In Octopus mode Octopus's own dispatch sensor has the schedule's slots; only boosts are ours.
        schedule = {**self.schedule, "enabled": False} if self.octopus_mode else self.schedule
        slots = planned_slots(schedule, self.run, dt_util.now(), self.charge_rate, self.overrides, self.boosts)
        return [
            {
                "start": start.isoformat(timespec="seconds"),
                "end": end.isoformat(timespec="seconds"),
                "charge_in_kwh": round(kwh, 2),
                "source": DISPATCH_SOURCE,
                "location": "AT_HOME",
            }
            for start, end, kwh in slots
        ]

    def _update_boosts(self, now: datetime) -> bool:
        """Track boosts' progress and drop finished ones. True if one that should end in Stopped ended."""
        session = self._float_state(self.session_entity)
        unplugged = self._state(self.plug_entity) in UNPLUGGED
        boosting = self._state(self.status_entity) == "Boosting"
        kept = []
        for boost in self.boosts:
            boost.track(session)
            idle_manual = (
                boost.kind == BOOST_MANUAL
                and boost.sent
                and not boosting
                and now - datetime.fromisoformat(boost.started) > MANUAL_BOOST_GRACE
            )
            if not (boost.finished(now) or unplugged or idle_manual):
                kept.append(boost)
        ended = [boost for boost in self.boosts if boost not in kept]
        self.boosts = kept
        return any(boost.stop_after for boost in ended)

    async def async_set_enabled(self, enabled: bool) -> None:
        self.schedule = {**self.schedule, "enabled": enabled}
        await self.async_update()

    def status(self) -> dict[str, Any]:
        now = dt_util.now()
        if self.octopus_mode:
            return {
                "state": self.decision.state,
                "mode": self.control_mode,
                "last_action": self.last_action,
                "upcoming": octopus_upcoming(self.schedule, now, self.overrides),
                "octopus": self.octopus_target,
                "octopus_battery_kwh": self.octopus_battery,
            }
        return {
            "state": self.decision.state,
            "mode": self.control_mode,
            "last_action": self.last_action,
            "run_date": self.run.date if self.run else None,
            "planned_kwh": self.run.kwh if self.run else None,
            "delivered_kwh": round(self.run.delivered, 2) if self.run else None,
            "remaining_kwh": round(self.decision.remaining, 2),
            "current_interval": self.decision.interval,
            "next_interval": next_interval(self.schedule, now, self.overrides),
            "upcoming": upcoming(self.schedule, now, self.overrides),
        }

    async def _async_tick(self, _now: datetime) -> None:
        await self.async_update()

    async def _async_state_changed(self, _event: Event) -> None:
        await self.async_update()

    async def async_update(self) -> None:
        async with self._lock:
            now = dt_util.now()
            # Yesterday's override can still matter while its charge overruns past midnight.
            oldest = (now.date() - timedelta(days=1)).isoformat()
            self.overrides = {day: o for day, o in self.overrides.items() if day >= oldest}
            stop_after_boost = self._update_boosts(now)
            try:
                if stop_after_boost:
                    await self._async_set_stopped()
                await self._async_send_boosts(now)
                if self.octopus_mode:
                    self.run = None
                    self.decision = Decision(STATE_OCTOPUS if self.schedule["enabled"] else STATE_DISABLED)
                    await self._async_sync_octopus(now)
                else:
                    self.run, self.decision = advance(
                        self.schedule, self.run, now, self._float_state(self.session_entity), self.overrides
                    )
                    await self._async_apply(now)
            except HomeAssistantError:
                # Try again on the next tick rather than stopping the schedule.
                _LOGGER.exception("Failed to control the charger")
            self._store.async_delay_save(self._data, 5)
            for listener in list(self._listeners):
                listener()

    async def _async_apply(self, now: datetime) -> None:
        decision = self.decision
        boosting = self._state(self.status_entity) == "Boosting"
        plug = self._state(self.plug_entity)
        plugged = plug is not None and plug not in UNPLUGGED

        if decision.charge:
            retry_due = self._last_boost_call is None or now - self._last_boost_call >= BOOST_RETRY
            if plugged and not boosting and retry_due:
                # Set even while only switching mode, so the slot's end still puts it back to Stopped.
                self._boost_started = True
                if not await self._async_prepare_boost(now):
                    return
                if self._last_boost_call is not None:
                    _LOGGER.warning(
                        "The zappi didn't start boosting after the last request; asking again. "
                        "Check the car is plugged in and accepting charge"
                    )
                amount = min(MAX_BOOST_KWH, max(MIN_BOOST_KWH, math.ceil(decision.remaining)))
                self._last_boost_call = now
                self._record(now, f"Scheduled boost of {amount} kWh")
                await self._async_call(MYENERGI_DOMAIN, "myenergi_boost", {"amount": amount})
        elif self._boost_started:
            # The slot ended or the kWh are in.
            self._boost_started = False
            self._last_boost_call = None
            self._mode_switched_at = None
            if boosting:
                self._record(now, f"Stopped scheduled boost ({decision.state})")
                await self._async_call(MYENERGI_DOMAIN, "myenergi_stop_boost", {})
            if self.switch_to_stopped:
                # Otherwise the zappi carries on in Eco+ (from surplus solar) outside the slots.
                await self._async_set_stopped()

    async def _async_prepare_boost(self, now: datetime) -> bool:
        """Whether the zappi can be sent a boost now, switching it to Eco+ first if needed.

        A zappi only boosts in Eco or Eco+ and takes a while to change mode, so after switching,
        wait MODE_SETTLE and check it reports the new mode before boosting. If it still doesn't,
        switch again.
        """
        if self._mode_switched_at is not None and now - self._mode_switched_at < MODE_SETTLE:
            return False
        if self._state(self.mode_entity) in BOOST_MODES:
            self._mode_switched_at = None
            return True
        self._mode_switched_at = now
        self._record(now, "Switched to Eco+ to boost")
        await self._async_call("select", "select_option", {"option": "Eco+"})
        return False

    async def _async_send_boosts(self, now: datetime) -> None:
        """Send card boosts that were waiting for the zappi to switch to Eco+."""
        for boost in self.boosts:
            if boost.sent or not await self._async_prepare_boost(now):
                continue
            boost.sent = True
            # The boost's clock starts when the zappi gets it.
            boost.started = now.isoformat()
            if boost.ready_by is not None:
                when = datetime.fromisoformat(boost.ready_by).strftime("%H:%M")
                self._record(now, f"Smart boost of {boost.kwh:g} kWh by {when}")
                await self._async_call(
                    MYENERGI_DOMAIN, "myenergi_smart_boost", {"amount": boost.kwh, "when": when}
                )
            else:
                self._record(now, f"Boost of {boost.kwh:g} kWh")
                await self._async_call(MYENERGI_DOMAIN, "myenergi_boost", {"amount": boost.kwh})

    async def _async_set_stopped(self) -> None:
        if self._state(self.mode_entity) not in (None, "Stopped"):
            self._record(dt_util.now(), "Switched to Stopped")
            await self._async_call("select", "select_option", {"option": "Stopped"})

    def _record(self, now: datetime, action: str) -> None:
        _LOGGER.info(action)
        self.last_action = {"time": now.isoformat(timespec="seconds"), "action": action}

    async def _async_sync_octopus(self, now: datetime) -> None:
        """Keep Octopus's charge target and ready-by time set for the upcoming charge."""
        charge = octopus_upcoming(self.schedule, now, self.overrides) if self.schedule["enabled"] else None
        if charge is None:
            self.octopus_target = None
            return
        percent = octopus_percent(charge["kwh"], self.octopus_battery)
        ready_by = charge["ready_by"]
        self.octopus_target = {"date": charge["date"], "percent": percent, "ready_by": ready_by}
        if not self.octopus_target_entity or not self.octopus_time_entity:
            return

        current_percent = self._float_state(self.octopus_target_entity)
        # A time entity's state is "HH:MM:SS", a select's option is "HH:MM".
        current_time = (self._state(self.octopus_time_entity) or "")[:5]
        if current_percent == percent and current_time == ready_by:
            self._octopus_last_set = None
            return
        wanted = (percent, ready_by)
        if (
            self._octopus_last_set is not None
            and self._octopus_last_set[:2] == wanted
            and now - self._octopus_last_set[2] < OCTOPUS_RETRY
        ):
            return  # just set; wait for Octopus's entities to catch up
        self._octopus_last_set = (*wanted, now)

        if current_percent != percent:
            _LOGGER.info("Setting Octopus charge target to %s%% for %s", percent, charge["date"])
            await self._async_call("number", "set_value", {"value": percent}, self.octopus_target_entity)
        if current_time != ready_by:
            _LOGGER.info("Setting Octopus ready-by time to %s", ready_by)
            if self.octopus_time_entity.startswith("select."):
                await self._async_call("select", "select_option", {"option": ready_by}, self.octopus_time_entity)
            else:
                await self._async_call("time", "set_value", {"time": f"{ready_by}:00"}, self.octopus_time_entity)

    async def _async_call(
        self, domain: str, service: str, data: dict[str, Any], entity_id: str | None = None
    ) -> None:
        await self.hass.services.async_call(
            domain, service, data, target={"entity_id": entity_id or self.mode_entity}, blocking=True
        )

    def _state(self, entity_id: str) -> str | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        return state.state

    def _float_state(self, entity_id: str) -> float | None:
        try:
            return float(self._state(entity_id))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def _data(self) -> dict[str, Any]:
        return {
            "schedule": self.schedule,
            "overrides": self.overrides,
            "boosts": [boost.as_dict() for boost in self.boosts],
            "run": self.run.as_dict() if self.run else None,
            "boost_started": self._boost_started,
        }
